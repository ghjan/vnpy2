# encoding: UTF-8

'''
本文件中包含的是CTA模块的回测引擎，回测引擎的API和CTA引擎一致，
可以使用和实盘相同的代码进行回测。
'''
from __future__ import division

import sys
import os
cta_engine_path = os.path.abspath(os.path.dirname(__file__))

from datetime import datetime, timedelta
from collections import OrderedDict
from itertools import product
import multiprocessing
import pymongo
#import MySQLdb
import json
import sys
import pickle as cPickle
import csv
import copy
import pandas as pd
import re
import traceback
import decimal
import numpy as np

from vnpy.trader.app.ctaStrategy.ctaBase import *
from vnpy.trader.vtConstant import *
from vnpy.trader.vtGateway import VtOrderData, VtTradeData
from vnpy.trader.vtFunction import loadMongoSetting
from vnpy.trader.vtEvent import *
from vnpy.trader.setup_logger import setup_logger
from vnpy.trader.data_source import DataSource
from vnpy.trader.app.ctaStrategy.ctaEngine import PositionBuffer

########################################################################
class BacktestingEngine(object):
    """
    CTA回测引擎
    函数接口和策略引擎保持一样，
    从而实现同一套代码从回测到实盘。
    # modified by IncenseLee：
    1.增加Mysql数据库的支持；
    2.修改装载数据为批量式后加载模式。
    3.增加csv 读取bar的回测模式
    4.增加csv 读取tick合并价差的回测模式
    5.增加EventEngine，并对newBar增加发送OnBar事件，供外部的回测主体显示Bar线。
    """
    
    TICK_MODE = 'tick'              # 数据模式，逐Tick回测
    BAR_MODE = 'bar'                # 数据模式，逐Bar回测

    REALTIME_MODE ='RealTime'       # 逐笔交易计算资金，供策略获取资金容量，计算开仓数量
    FINAL_MODE = 'Final'            # 最后才统计交易，不适合按照百分比等开仓数量计算

    #----------------------------------------------------------------------
    def __init__(self, eventEngine = None):
        """Constructor"""

        self.eventEngine = eventEngine

        # 本地停止单编号计数
        self.stopOrderCount = 0
        # stopOrderID = STOPORDERPREFIX + str(stopOrderCount)
        
        # 本地停止单字典
        # key为stopOrderID，value为stopOrder对象
        self.stopOrderDict = {}             # 停止单撤销后不会从本字典中删除
        self.workingStopOrderDict = {}      # 停止单撤销后会从本字典中删除

        # 引擎类型为回测
        self.engineType = ENGINETYPE_BACKTESTING

        # 回测相关
        self.strategy = None        # 回测策略
        self.mode = self.BAR_MODE   # 回测模式，默认为K线
        self.strategy_name = 'strategy_{}'.format(datetime.now().strftime('%M%S'))  # 回测策略的实例名字
        self.daily_report_name = EMPTY_STRING   # 策略的日净值报告文件名称

        self.startDate = ''
        self.initDays = 0
        self.endDate = ''

        self.slippage = 0           # 回测时假设的滑点
        self.rate = 0               # 回测时假设的佣金比例（适用于百分比佣金）
        self.size = 1               # 合约大小，默认为1        
        self.priceTick = 0          # 价格最小变动

        self.dbClient = None        # 数据库客户端
        self.dbCursor = None        # 数据库指针
        
        self.historyData = []       # 历史数据的列表，回测用
        self.initData = []          # 初始化用的数据
        self.backtestingData = []   # 回测用的数据
        
        self.dbName = ''            # 回测数据库名
        self.symbol = ''            # 回测集合名
        self.margin_rate = 0.11     # 回测合约的保证金比率

        self.dataStartDate = None       # 回测数据开始日期，datetime对象
        self.dataEndDate = None         # 回测数据结束日期，datetime对象
        self.strategyStartDate = None   # 策略启动日期（即前面的数据用于初始化），datetime对象
        
        self.limitOrderDict = OrderedDict()         # 限价单字典
        self.workingLimitOrderDict = OrderedDict()  # 活动限价单字典，用于进行撮合用
        self.limitOrderCount = 0                    # 限价单编号

        # 持仓缓存字典
        # key为vtSymbol，value为PositionBuffer对象
        self.posBufferDict = {}

        self.tradeCount = 0                 # 成交编号
        self.tradeDict = OrderedDict()      # 成交字典
        self.longPosition = []              # 多单持仓
        self.shortPosition = []             # 空单持仓

        self.logList = []                   # 日志记录
        
        # 当前最新数据，用于模拟成交用
        self.tick = None
        self.bar = None
        self.dt = None                      # 最新的时间
        self.gatewayName = u'BackTest'

        self.last_leg1_tick = None
        self.last_leg2_tick = None
        self.last_bar = None
        self.is_7x24 = False

        # csvFile相关
        self.barTimeInterval = 60          # csv文件，属于K线类型，K线的周期（秒数）,缺省是1分钟

        # 费用情况

        self.percent = EMPTY_FLOAT
        self.percentLimit = 30              # 投资仓位比例上限

        # 回测计算相关
        self.calculateMode = self.FINAL_MODE
        self.usageCompounding = False       # 是否使用简单复利 （只针对FINAL_MODE有效）

        self.initCapital = 1000000            # 期初资金
        self.capital = self.initCapital     # 资金
        self.netCapital = self.initCapital  # 实时资金净值（每日根据capital和持仓浮盈计算）
        self.maxCapital = self.initCapital          # 资金最高净值
        self.maxNetCapital = self.initCapital
        self.avaliable =  self.initCapital

        self.maxPnl = 0                     # 最高盈利
        self.minPnl = 0                     # 最大亏损
        self.maxVolume = 1                  # 最大仓位数
        self.winningResult = 0              # 盈利次数
        self.losingResult = 0              # 亏损次数

        self.totalResult = 0         # 总成交数量
        self.totalWinning = 0        # 总盈利
        self.totalLosing = 0        # 总亏损
        self.totalTurnover = 0       # 总成交金额（合约面值）
        self.totalCommission = 0     # 总手续费
        self.totalSlippage = 0       # 总滑点

        self.timeList = []           # 时间序列
        self.pnlList = []            # 每笔盈亏序列
        self.capitalList = []        # 盈亏汇总的时间序列
        self.drawdownList = []       # 回撤的时间序列
        self.drawdownRateList = []   # 最大回撤比例的时间序列(成交结算）

        self.maxNetCapital_time = ''
        self.max_drowdown_rate_time = ''
        self.daily_max_drawdown_rate = 0    # 按照日结算价计算

        self.dailyList = []
        self.daily_first_benchmark = None

        self.exportTradeList = []    # 导出交易记录列表
        self.export_wenhua_signal = False
        self.fixCommission = EMPTY_FLOAT    # 固定交易费用

        self.logger = None

        self.useBreakoutMode = False

    def getAccountInfo(self):
        """返回账号的实时权益，可用资金，仓位比例,投资仓位比例上限"""
        if self.netCapital == EMPTY_FLOAT:
            self.percent = EMPTY_FLOAT

        return self.netCapital, self.avaliable, self.percent, self.percentLimit

    #----------------------------------------------------------------------
    def setStartDate(self, startDate='20100416', initDays=10):
        """设置回测的启动日期"""
        self.startDate = startDate
        self.initDays = initDays

        self.dataStartDate = datetime.strptime(startDate, '%Y%m%d')

        # 初始化天数
        initTimeDelta = timedelta(initDays)

        self.strategyStartDate = self.dataStartDate + initTimeDelta
        
    #----------------------------------------------------------------------
    def setEndDate(self, endDate=''):
        """设置回测的结束日期"""
        self.endDate = endDate
        if endDate:
            self.dataEndDate = datetime.strptime(endDate, '%Y%m%d')
            # 若不修改时间则会导致不包含dataEndDate当天数据
            self.dataEndDate.replace(hour=23, minute=59)
        else:
            self.dataEndDate = datetime.now()

    def setMinDiff(self, minDiff):
        """设置回测品种的最小跳价，用于修正数据"""
        self.minDiff = minDiff
        self.priceTick = minDiff

    #----------------------------------------------------------------------
    def setBacktestingMode(self, mode):
        """设置回测模式"""
        self.mode = mode

    #----------------------------------------------------------------------
    def setDatabase(self, dbName, symbol):
        """设置历史数据所用的数据库"""
        self.dbName = dbName
        self.symbol = symbol

    def setMarginRate(self, margin_rate):

        if margin_rate!= EMPTY_FLOAT:
            self.margin_rate = margin_rate

    def qryMarginRate(self,symbol):
        """
        根据合约symbol，返回其保证金比率
        :param symbol: 
        :return: 
        """
        return self.margin_rate

    # ----------------------------------------------------------------------
    def setSlippage(self, slippage):
        """设置滑点点数"""
        self.slippage = slippage

    # ----------------------------------------------------------------------
    def setSize(self, size):
        """设置合约大小"""
        self.size = size

    def qrySize(self,symbol):
        """查询合约的size"""
        return self.size

    # ----------------------------------------------------------------------
    def setRate(self, rate):
        """设置佣金比例"""
        self.rate = rate

    # ----------------------------------------------------------------------
    def setPriceTick(self, priceTick):
        """设置价格最小变动"""
        self.priceTick = priceTick
        self.minDiff = priceTick

    def setStrategyName(self,strategy_name):
        """
        设置策略的运行实例名称
        :param strategy_name: 
        :return: 
        """
        self.strategy_name = strategy_name

    def setDailyReportName(self, report_file):
        """
        设置策略的日净值记录csv保存文件名（含路径）
        :param report_file: 保存文件名（含路径）
        :return: 
        """
        self.daily_report_name = report_file
    #----------------------------------------------------------------------
    def connectMysql(self):
        """连接MysqlDB"""

        # 载入json文件
        fileName = 'mysql_connect.json'
        try:
            f = open(fileName,'r',encoding='utf8')
        except IOError:
            self.writeCtaLog(u'回测引擎读取Mysql_connect.json失败')
            return

        # 解析json文件
        setting = json.load(f)
        try:
            mysql_host = str(setting['host'])
            mysql_port = int(setting['port'])
            mysql_user = str(setting['user'])
            mysql_passwd = str(setting['passwd'])
            mysql_db = str(setting['db'])

        except IOError:
            self.writeCtaLog(u'回测引擎读取Mysql_connect.json,连接配置缺少字段，请检查')
            return

        try:
            self.__mysqlConnection = MySQLdb.connect(host=mysql_host, user=mysql_user,
                                                     passwd=mysql_passwd, db=mysql_db, port=mysql_port)
            self.__mysqlConnected = True
            self.writeCtaLog(u'回测引擎连接MysqlDB成功')
        except Exception:
            self.writeCtaLog(u'回测引擎连接MysqlDB失败')

     #----------------------------------------------------------------------
    def loadDataHistoryFromMysql(self, symbol, startDate, endDate):
        """载入历史TICK数据
        如果加载过多数据会导致加载失败,间隔不要超过半年
        """

        if not endDate:
            endDate = datetime.today()

        # 看本地缓存是否存在
        if self.__loadDataHistoryFromLocalCache(symbol, startDate, endDate):
            self.writeCtaLog(u'历史TICK数据从Cache载入')
            return

        # 每次获取日期周期
        intervalDays = 10

        for i in range (0,(endDate - startDate).days +1, intervalDays):
            d1 = startDate + timedelta(days = i )

            if (endDate - d1).days > 10:
                d2 = startDate + timedelta(days = i + intervalDays -1 )
            else:
                d2 = endDate

            # 从Mysql 提取数据
            self.__qryDataHistoryFromMysql(symbol, d1, d2)

        self.writeCtaLog(u'历史TICK数据共载入{0}条'.format(len(self.historyData)))

        # 保存本地cache文件
        self.__saveDataHistoryToLocalCache(symbol, startDate, endDate)

    def runBackTestingWithMongoDBTicks(self, symbol):
        """
        根据测试的每一天，从MongoDB载入历史数据，并推送Tick至回测函数
        """

        self.capital = self.initCapital  # 更新设置期初资金

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return
        else:
            dataClass = CtaTickData
            func = self.newTick

        self.output(u'开始回测')

        # self.strategy.inited = True
        self.strategy.onInit()
        self.output(u'策略初始化完成')

        self.strategy.trading = True
        self.strategy.onStart()
        self.output(u'策略启动完成')

        # isOffline = False  # WJ
        isOffline = True
        host, port, log = loadMongoSetting()

        self.dbClient = pymongo.MongoClient(host, port)
        symbol = self.strategy.shortSymbol + self.symbol[-2:]
        self.strategy.vtSymbol = symbol
        collection = self.dbClient[self.dbName][symbol]

        self.output(u'开始载入数据')

        # 载入回测数据
        if not self.dataEndDate:
            self.dataEndDate = datetime.now()

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        # 循环每一天
        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)

            # 看本地缓存是否存在
            cachefilename = u'{0}_{1}_{2}'.format(self.symbol, symbol, testday.strftime('%Y%m%d'))
            rawTicks = self.__loadTicksFromLocalCache(cachefilename)

            dt = None

            if len(rawTicks) < 1 and isOffline == False:

                testday_monrning = testday  # testday.replace(hour=0, minute=0, second=0, microsecond=0)
                testday_midnight = testday + timedelta(
                    days=1)  # testday.replace(hour=23, minute=59, second=59, microsecond=999999)

                query_time = datetime.now()
                # 载入初始化需要用的数据
                flt = {'tradingDay': testday.strftime('%Y%m%d')} # WJ: using TradingDay instead of calandar day
                # flt = {'datetime': {'$gte': testday_monrning, '$lt': testday_midnight}}
                initCursor = collection.find(flt).sort('datetime', pymongo.ASCENDING)

                process_time = datetime.now()
                # 将数据从查询指针中读取出，并生成列表
                count_ticks = 0

                for d in initCursor:
                    data = dataClass()
                    data.__dict__ = d
                    rawTicks.append(data)
                    count_ticks += 1

                self.writeCtaLog(u'回测日期{0}，数据量：{1}，查询耗时:{2},回测耗时:{3}'
                            .format(testday.strftime('%Y-%m-%d'), count_ticks, str(datetime.now() - query_time),
                                    str(datetime.now() - process_time)))

                # 保存本地cache文件
                if count_ticks > 0:
                    self.__saveTicksToLocalCache(cachefilename, rawTicks)

            for t in rawTicks:
                # 排除涨停/跌停的数据
                if ((t.askPrice1 == float('1.79769E308') or t.askPrice1 == 0) and t.askVolume1 == 0) \
                        or ((t.bidPrice1 == float('1.79769E308') or t.bidPrice1 == 0) and t.bidVolume1 == 0):
                    continue

                # 推送到策略中
                self.newTick(t)

                # 保存最后一个Tick，确保savingDailyData()工作正常
                self.last_leg1_tick = t
                self.last_leg1_tick.vtSymbol = symbol

            # 记录每日净值
            if len(rawTicks) > 1:
                self.savingDailyData(testday, self.capital, self.maxCapital, self.totalCommission)

    def __loadDataHistoryFromLocalCache(self, symbol, startDate, endDate):
        """看本地缓存是否存在
        added by IncenseLee
        """

        # 运行路径下cache子目录
        cacheFolder = os.getcwd()+'/cache'

        # cache文件
        cacheFile = u'{0}/{1}_{2}_{3}.pickle'.\
                    format(cacheFolder, symbol, startDate.strftime('%Y-%m-%d'), endDate.strftime('%Y-%m-%d'))

        if not os.path.isfile(cacheFile):
            return False

        else:
            try:
                # 从cache文件加载
                cache = open(cacheFile,mode='r')
                self.historyData = cPickle.load(cache)
                cache.close()
                return True
            except Exception as e:
                self.writeCtaLog(u'读取文件{0}失败'.format(cacheFile))
                return False

    def __saveDataHistoryToLocalCache(self, symbol, startDate, endDate):
        """保存本地缓存
        added by IncenseLee
        """

        # 运行路径下cache子目录
        cacheFolder = os.getcwd()+'/cache'

        # 创建cache子目录
        if not os.path.isdir(cacheFolder):
            os.mkdir(cacheFolder)

        # cache 文件名
        cacheFile = u'{0}/{1}_{2}_{3}.pickle'.\
                    format(cacheFolder, symbol, startDate.strftime('%Y-%m-%d'), endDate.strftime('%Y-%m-%d'))

        # 重复存在 返回
        if os.path.isfile(cacheFile):
            return False

        else:
            # 写入cache文件
            cache = open(cacheFile, mode='w')
            cPickle.dump(self.historyData,cache)
            cache.close()
            return True

    #----------------------------------------------------------------------
    def __qryDataHistoryFromMysql(self, symbol, startDate, endDate):
        """从Mysql载入历史TICK数据
        added by IncenseLee
        """

        try:
            self.connectMysql()
            if self.__mysqlConnected:

                # 获取指针
                cur = self.__mysqlConnection.cursor(MySQLdb.cursors.DictCursor)

                if endDate:

                    # 开始日期 ~ 结束日期
                    sqlstring = ' select \'{0}\' as InstrumentID, str_to_date(concat(ndate,\' \', ntime),' \
                               '\'%Y-%m-%d %H:%i:%s\') as UpdateTime,price as LastPrice,vol as Volume, day_vol as DayVolume,' \
                               'position_vol as OpenInterest,bid1_price as BidPrice1,bid1_vol as BidVolume1, ' \
                               'sell1_price as AskPrice1, sell1_vol as AskVolume1 from TB_{0}MI ' \
                               'where ndate between cast(\'{1}\' as date) and cast(\'{2}\' as date) order by UpdateTime'.\
                               format(symbol,  startDate, endDate)

                elif startDate:

                    # 开始日期 - 当前
                    sqlstring = ' select \'{0}\' as InstrumentID,str_to_date(concat(ndate,\' \', ntime),' \
                               '\'%Y-%m-%d %H:%i:%s\') as UpdateTime,price as LastPrice,vol as Volume, day_vol as DayVolume,' \
                               'position_vol as OpenInterest,bid1_price as BidPrice1,bid1_vol as BidVolume1, ' \
                               'sell1_price as AskPrice1, sell1_vol as AskVolume1 from TB__{0}MI ' \
                               'where ndate > cast(\'{1}\' as date) order by UpdateTime'.\
                               format( symbol, startDate)

                else:

                    # 所有数据
                    sqlstring =' select \'{0}\' as InstrumentID,str_to_date(concat(ndate,\' \', ntime),' \
                              '\'%Y-%m-%d %H:%i:%s\') as UpdateTime,price as LastPrice,vol as Volume, day_vol as DayVolume,' \
                              'position_vol as OpenInterest,bid1_price as BidPrice1,bid1_vol as BidVolume1, ' \
                              'sell1_price as AskPrice1, sell1_vol as AskVolume1 from TB__{0}MI order by UpdateTime'.\
                              format(symbol)

                self.writeCtaLog(sqlstring)

                # 执行查询
                count = cur.execute(sqlstring)
                self.writeCtaLog(u'历史TICK数据共{0}条'.format(count))


                # 分批次读取
                fetch_counts = 0
                fetch_size = 1000

                while True:
                    results = cur.fetchmany(fetch_size)

                    if not results:
                        break

                    fetch_counts = fetch_counts + len(results)

                    if not self.historyData:
                        self.historyData =results

                    else:
                        self.historyData = self.historyData + results

                    self.writeCtaLog(u'{1}~{2}历史TICK数据载入共{0}条'.format(fetch_counts,startDate,endDate))


            else:
                self.writeCtaLog(u'MysqlDB未连接，请检查')

        except MySQLdb.Error as e:
            self.writeCtaLog(u'MysqlDB载入数据失败，请检查.Error {0}'.format(e))

    def __dataToTick(self, data):
        """
        数据库查询返回的data结构，转换为tick对象
        added by IncenseLee        """

        tick = CtaTickData()
        symbol = data['InstrumentID']
        tick.symbol = symbol

        # 创建TICK数据对象并更新数据
        tick.vtSymbol = symbol
        # tick.openPrice = data['OpenPrice']
        # tick.highPrice = data['HighestPrice']
        # tick.lowPrice = data['LowestPrice']
        tick.lastPrice = float(data['LastPrice'])

        # bug fix:
        # ctp日常传送的volume数据，是交易日日内累加值。数据库的volume，是数据商自行计算整理的
        # 因此，改为使用DayVolume，与CTP实盘一致
        #tick.volume = data['Volume']
        tick.volume = data['DayVolume']
        tick.openInterest = data['OpenInterest']

        #  tick.upperLimit = data['UpperLimitPrice']
        #  tick.lowerLimit = data['LowerLimitPrice']

        tick.datetime = data['UpdateTime']
        tick.date = tick.datetime.strftime('%Y-%m-%d')
        tick.time = tick.datetime.strftime('%H:%M:%S')
        # 数据库中并没有tradingDay的数据，回测时，暂时按照date授予。
        tick.tradingDay = tick.date

        tick.bidPrice1 = float(data['BidPrice1'])
        # tick.bidPrice2 = data['BidPrice2']
        # tick.bidPrice3 = data['BidPrice3']
        # tick.bidPrice4 = data['BidPrice4']
        # tick.bidPrice5 = data['BidPrice5']

        tick.askPrice1 = float(data['AskPrice1'])
        # tick.askPrice2 = data['AskPrice2']
        # tick.askPrice3 = data['AskPrice3']
        # tick.askPrice4 = data['AskPrice4']
        # tick.askPrice5 = data['AskPrice5']

        tick.bidVolume1 = data['BidVolume1']
        # tick.bidVolume2 = data['BidVolume2']
        # tick.bidVolume3 = data['BidVolume3']
        # tick.bidVolume4 = data['BidVolume4']
        # tick.bidVolume5 = data['BidVolume5']

        tick.askVolume1 = data['AskVolume1']
        # tick.askVolume2 = data['AskVolume2']
        # tick.askVolume3 = data['AskVolume3']
        # tick.askVolume4 = data['AskVolume4']
        # tick.askVolume5 = data['AskVolume5']

        return tick

    def __barToTick(self, bar):
        """
        数据库查询返回的bar结构，转换为tick对象
        added by Wenjian Du """

        # TODO
        tick = CtaTickData()
        tick.symbol = bar.symbol

        # 创建TICK数据对象并更新数据
        tick.vtSymbol = bar.symbol
        # tick.openPrice = data['OpenPrice']
        # tick.highPrice = data['HighestPrice']
        # tick.lowPrice = data['LowestPrice']
        tick.lastPrice = float(bar.close)

        # bug fix:
        # ctp日常传送的volume数据，是交易日日内累加值。数据库的volume，是数据商自行计算整理的
        # 因此，改为使用DayVolume，与CTP实盘一致
        tick.volume = bar.volume
        tick.openInterest = bar.openInterest

        #  tick.upperLimit = data['UpperLimitPrice']
        #  tick.lowerLimit = data['LowerLimitPrice']

        tick.datetime = bar.datetime + timedelta(seconds=self.barTimeInterval)
        tick.date = tick.datetime.strftime('%Y-%m-%d')
        tick.time = tick.datetime.strftime('%H:%M:%S')
        # 数据库中并没有tradingDay的数据，回测时，暂时按照date授予。
        tick.tradingDay = bar.tradingDay

        tick.bidPrice1 = float(bar.close)
        # tick.bidPrice2 = data['BidPrice2']
        # tick.bidPrice3 = data['BidPrice3']
        # tick.bidPrice4 = data['BidPrice4']
        # tick.bidPrice5 = data['BidPrice5']

        tick.askPrice1 = float(bar.close)
        # tick.askPrice2 = data['AskPrice2']
        # tick.askPrice3 = data['AskPrice3']
        # tick.askPrice4 = data['AskPrice4']
        # tick.askPrice5 = data['AskPrice5']

        tick.bidVolume1 = bar.volume
        # tick.bidVolume2 = data['BidVolume2']
        # tick.bidVolume3 = data['BidVolume3']
        # tick.bidVolume4 = data['BidVolume4']
        # tick.bidVolume5 = data['BidVolume5']

        tick.askVolume1 = bar.volume
        # tick.askVolume2 = data['AskVolume2']
        # tick.askVolume3 = data['AskVolume3']
        # tick.askVolume4 = data['AskVolume4']
        # tick.askVolume5 = data['AskVolume5']

        return tick

    #----------------------------------------------------------------------
    def getMysqlDeltaDate(self,symbol, startDate, decreaseDays):
        """从mysql库中获取交易日前若干天
        added by IncenseLee
        """
        try:
            if self.__mysqlConnected:

                # 获取mysql指针
                cur = self.__mysqlConnection.cursor()

                sqlstring='select distinct ndate from TB_{0}MI where ndate < ' \
                          'cast(\'{1}\' as date) order by ndate desc limit {2},1'.format(symbol, startDate, decreaseDays-1)

                # self.writeCtaLog(sqlstring)

                count = cur.execute(sqlstring)

                if count > 0:

                    # 提取第一条记录
                    result = cur.fetchone()

                    return result[0]

                else:
                    self.writeCtaLog(u'MysqlDB没有查询结果，请检查日期')

            else:
                self.writeCtaLog(u'MysqlDB未连接，请检查')

        except MySQLdb.Error as e:
            self.writeCtaLog(u'MysqlDB载入数据失败，请检查.Error {0}: {1}'.format(e.arg[0],e.arg[1]))

        # 出错后缺省返回
        return startDate-timedelta(days=3)

    # ----------------------------------------------------------------------
    def runBackTestingWithArbTickFile(self,mainPath, arbSymbol):
        """运行套利回测（使用本地tick TXT csv数据)
        参数：套利代码 SP rb1610&rb1701
        added by IncenseLee
        原始的tick，分别存放在白天目录1和夜盘目录2中，每天都有各个合约的数据
        Z:\ticks\SHFE\201606\RB\0601\
                                     RB1610.txt
                                     RB1701.txt
                                     ....
        Z:\ticks\SHFE_night\201606\RB\0601
                                     RB1610.txt
                                     RB1701.txt
                                     ....

        夜盘目录为自然日，不是交易日。

        按照回测的开始日期，到结束日期，循环每一天。
        每天优先读取日盘数据，再读取夜盘数据。
        读取eg1（如RB1610），读取Leg2（如RB701），合并成价差tick，灌输到策略的onTick中。
        """
        self.capital = self.initCapital  # 更新设置期初资金

        if len(arbSymbol) < 1:
            self.writeCtaLog(u'套利合约为空')
            return

        if not (arbSymbol.upper().index("SP") == 0 and arbSymbol.index(" ") > 0 and arbSymbol.index("&") > 0):
            self.writeCtaLog(u'套利合约格式不符合')
            return

        # 获得Leg1，leg2
        legs = arbSymbol[arbSymbol.index(" "):]
        leg1 = legs[1:legs.index("&")]
        leg2 = legs[legs.index("&") + 1:]
        self.writeCtaLog(u'Leg1:{0},Leg2:{1}'.format(leg1, leg2))

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol)<1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        #首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):

            testday = self.dataStartDate + timedelta(days = i)

            self.output(u'回测日期:{0}'.format(testday))

            # 白天数据
            self.__loadArbTicks(mainPath,testday,leg1,leg2)

            # 撤销所有之前的orders
            if self.symbol:
                self.cancelOrders(self.symbol)

            # 夜盘数据
            self.__loadArbTicks(mainPath+'_night', testday, leg1, leg2)


    def __loadArbTicks(self,mainPath,testday,leg1,leg2):

        self.writeCtaLog(u'加载回测日期:{0}\{1}的价差tick'.format(mainPath, testday))

        cachefilename = u'{0}_{1}_{2}_{3}_{4}'.format(self.symbol,leg1,leg2, mainPath, testday.strftime('%Y%m%d'))

        arbTicks = self.__loadArbTicksFromLocalCache(cachefilename)

        dt = None

        if len(arbTicks) < 1:

            leg1File = u'z:\\ticks\\{0}\\{1}\\{2}\\{3}\\{4}.txt' \
                .format(mainPath, testday.strftime('%Y%m'), self.symbol, testday.strftime('%m%d'), leg1)
            if not os.path.isfile(leg1File):
                self.writeCtaLog(u'{0}文件不存在'.format(leg1File))
                return

            leg2File = u'z:\\ticks\\{0}\\{1}\\{2}\\{3}\\{4}.txt' \
                .format(mainPath, testday.strftime('%Y%m'), self.symbol, testday.strftime('%m%d'), leg2)
            if not os.path.isfile(leg2File):
                self.writeCtaLog(u'{0}文件不存在'.format(leg2File))
                return

            # 先读取leg2的数据到目录，以日期时间为key
            leg2Ticks = {}

            leg2CsvReadFile = open(leg2File, 'rb')
            #reader = csv.DictReader((line.replace('\0',' ') for line in leg2CsvReadFile), delimiter=",")
            reader = csv.DictReader(leg2CsvReadFile, delimiter=",")
            self.writeCtaLog(u'加载{0}'.format(leg2File))
            for row in reader:
                tick = CtaTickData()

                tick.vtSymbol = self.symbol
                tick.symbol = self.symbol

                tick.date = testday.strftime('%Y%m%d')
                tick.tradingDay = tick.date
                tick.time = row['Time']

                try:
                    tick.datetime = datetime.strptime(tick.date + ' ' + tick.time, '%Y%m%d %H:%M:%S.%f')
                except Exception as ex:
                    self.writeCtaError(u'日期转换错误:{0},{1}:{2}'.format(tick.date + ' ' + tick.time, Exception, ex))
                    continue

                # 修正毫秒
                if tick.datetime.replace(microsecond = 0) == dt:
                    # 与上一个tick的时间（去除毫秒后）相同,修改为500毫秒
                    tick.datetime=tick.datetime.replace(microsecond = 500)
                    tick.time = tick.datetime.strftime('%H:%M:%S.%f')

                else:
                    tick.datetime = tick.datetime.replace(microsecond=0)
                    tick.time = tick.datetime.strftime('%H:%M:%S.%f')

                dt = tick.datetime

                tick.lastPrice = float(row['LastPrice'])
                tick.volume = int(float(row['LVolume']))
                tick.bidPrice1 = float(row['BidPrice'])  # 叫买价（价格低）
                tick.bidVolume1 = int(float(row['BidVolume']))
                tick.askPrice1 = float(row['AskPrice'])  # 叫卖价（价格高）
                tick.askVolume1 = int(float(row['AskVolume']))

                # 排除涨停/跌停的数据
                if (tick.bidPrice1 == float('1.79769E308') and tick.bidVolume1 == 0) \
                    or (tick.askPrice1 == float('1.79769E308') and tick.askVolume1 == 0):
                    continue

                dtStr = tick.date + ' ' + tick.time
                if dtStr in leg2Ticks:
                    pass
                    #self.writeCtaError(u'日内数据重复，异常,数据时间为:{0}'.format(dtStr))
                else:
                    leg2Ticks[dtStr] = tick

            leg1CsvReadFile = file(leg1File, 'rb')
            #reader = csv.DictReader((line.replace('\0',' ') for line in leg1CsvReadFile), delimiter=",")
            reader = csv.DictReader(leg1CsvReadFile, delimiter=",")
            self.writeCtaLog(u'加载{0}'.format(leg1File))

            dt = None
            for row in reader:

                arbTick = CtaTickData()

                arbTick.date = testday.strftime('%Y%m%d')
                arbTick.time = row['Time']
                try:
                    arbTick.datetime = datetime.strptime(arbTick.date + ' ' + arbTick.time, '%Y%m%d %H:%M:%S.%f')
                except Exception as ex:
                    self.writeCtaError(u'日期转换错误:{0},{1}:{2}'.format(arbTick.date + ' ' + arbTick.time, Exception, ex))
                    continue

                # 修正毫秒
                if arbTick.datetime.replace(microsecond=0) == dt:
                    # 与上一个tick的时间（去除毫秒后）相同,修改为500毫秒
                    arbTick.datetime = arbTick.datetime.replace(microsecond=500)
                    arbTick.time = arbTick.datetime.strftime('%H:%M:%S.%f')

                else:
                    arbTick.datetime = arbTick.datetime.replace(microsecond=0)
                    arbTick.time = arbTick.datetime.strftime('%H:%M:%S.%f')

                dt = arbTick.datetime
                dtStr = ' '.join([arbTick.date, arbTick.time])

                if dtStr in leg2Ticks:
                    leg2Tick = leg2Ticks[dtStr]

                    arbTick.vtSymbol = self.symbol
                    arbTick.symbol = self.symbol

                    arbTick.lastPrice = EMPTY_FLOAT
                    arbTick.volume = EMPTY_INT

                    leg1AskPrice1 = float(row['AskPrice'])
                    leg1AskVolume1 = int(float(row['AskVolume']))

                    leg1BidPrice1 = float(row['BidPrice'])
                    leg1BidVolume1 = int(float(row['BidVolume']))

                    # 排除涨停/跌停的数据
                    if ((leg1AskPrice1 == float('1.79769E308') or leg1AskPrice1 == 0) and leg1AskVolume1 == 0) \
                            or ((leg1BidPrice1 == float('1.79769E308') or leg1BidPrice1 == 0) and leg1BidVolume1 == 0):
                        continue

                    # 叫卖价差=leg1.askPrice1 - leg2.bidPrice1，volume为两者最小
                    arbTick.askPrice1 = leg1AskPrice1 - leg2Tick.bidPrice1
                    arbTick.askVolume1 = min(leg1AskVolume1, leg2Tick.bidVolume1)

                    # 叫买价差=leg1.bidPrice1 - leg2.askPrice1，volume为两者最小
                    arbTick.bidPrice1 = leg1BidPrice1 - leg2Tick.askPrice1
                    arbTick.bidVolume1 = min(leg1BidVolume1, leg2Tick.askVolume1)

                    arbTicks.append(arbTick)

                    del leg2Ticks[dtStr]

            # 保存到历史目录
            if len(arbTicks) > 0:
                self.__saveArbTicksToLocalCache(cachefilename, arbTicks)

        for t in arbTicks:
            # 推送到策略中
            self.newTick(t)

    def __loadArbTicksFromLocalCache(self, filename):
        """从本地缓存中，加载数据"""
        # 运行路径下cache子目录
        cacheFolder = os.getcwd() + '/cache'

        # cache文件
        cacheFile = u'{0}/{1}.pickle'. \
            format(cacheFolder, filename)

        if not os.path.isfile(cacheFile):
            return []
        else:
            # 从cache文件加载
            cache = open(cacheFile, mode='r')
            l = cPickle.load(cache)
            cache.close()
            return l

    def __saveArbTicksToLocalCache(self, filename, arbticks):
        """保存价差tick到本地缓存目录"""
        # 运行路径下cache子目录
        cacheFolder = os.getcwd() + '/cache'

        # 创建cache子目录
        if not os.path.isdir(cacheFolder):
            os.mkdir(cacheFolder)

        # cache 文件名
        cacheFile = u'{0}/{1}.pickle'. \
            format(cacheFolder, filename)

        # 重复存在 返回
        if os.path.isfile(cacheFile):
            return False

        else:
            # 写入cache文件
            cache = open(cacheFile, mode='w')
            cPickle.dump(arbticks, cache)
            cache.close()
            return True

    # ----------------------------------------------------------------------

    def runBackTestingWithTickFile(self, mainPath, symbol):
        """运行Tick回测（使用本地tick TXT csv数据)
        参数：代码 rb1610
        added by WenjianDu
        原始的tick，分别存放在白天目录1和夜盘目录2中，每天都有各个合约的数据
        Z:\ticks\SHFE\201606\RB\0601\
                                     RB1610.txt
                                     RB1701.txt
                                     ....
        Z:\ticks\SHFE_night\201606\RB\0601
                                     RB1610.txt
                                     RB1701.txt
                                     ....

        夜盘目录为自然日，不是交易日。

        按照回测的开始日期，到结束日期，循环每一天。
        每天优先读取日盘数据，再读取夜盘数据。
        读取tick（如RB1610），灌输到策略的onTick中。
        """
        self.capital = self.initCapital  # 更新设置期初资金

        if len(symbol) < 1:
            self.writeCtaLog(u'合约为空')
            return

        # 获得tick
        self.writeCtaLog(u'arbSymbol:{0}'.format(symbol))

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)
            self.output(u'回测日期:{0}'.format(testday))
            # 白天数据
            self.__loadTxtTicks(mainPath, testday, symbol)
            # 撤销所有之前的orders
            if self.symbol:
                self.cancelOrders(self.symbol)
            # # 夜盘数据
            # self.__loadTxtTicks(mainPath + '_night', testday, symbol)
            self.savingDailyData(testday, self.capital, self.maxCapital, self.totalCommission)

    def __loadTxtTicks(self, mainPath, testday, symbol):

        self.writeCtaLog(u'加载回测日期:{0}\{1}的tick'.format(mainPath, testday))

        cachefilename = u'{0}_{1}_{2}_{3}'.format(self.symbol, symbol, mainPath, testday.strftime('%Y%m%d'))

        rawTicks = self.__loadTicksFromLocalCache(cachefilename)

        dt = None

        if len(rawTicks) < 1:

            # rawFile = u'F:\\FutureData\\{0}\\{1}\\{2}\\{3}\\{4}.txt' \
            #     .format(mainPath, testday.strftime('%Y%m'), self.symbol, testday.strftime('%m%d'), symbol)
            rawFile = u'/home/wenjiand/Downloads/FutureData/{0}/{1}/{2}/{3}/{4}.txt' \
                .format(mainPath, testday.strftime('%Y%m'), self.strategy.shortSymbol, testday.strftime('%m%d'), self.strategy.symbol.upper())
            if not os.path.isfile(rawFile):
                self.writeCtaLog(u'{0}文件不存在'.format(rawFile))
                return

            # 先读取raw的数据到目录，以日期时间为key
            tempTicks = {}

            rawCsvReadFile = open(rawFile, 'r', encoding='utf8')
            # reader = csv.DictReader((line.replace('\0',' ') for line in rawCsvReadFile), delimiter=",")
            reader = csv.DictReader(rawCsvReadFile, delimiter=",")
            self.writeCtaLog(u'加载{0}'.format(rawFile))
            for row in reader:
                tick = CtaTickData()

                tick.symbol = self.symbol
                tick.vtSymbol = symbol

                tick.date = testday.strftime('%Y%m%d')
                tick.tradingDay = tick.date
                tick.time = row['Time']

                try:
                    tick.datetime = datetime.strptime(tick.date + ' ' + tick.time, '%Y%m%d %H:%M:%S.%f')
                except Exception as ex:
                    self.writeCtaError(u'日期转换错误:{0},{1}:{2}'.format(tick.date + ' ' + tick.time, Exception, ex))
                    continue

                # 修正毫秒
                if tick.datetime.replace(microsecond=0) == dt:
                    # 与上一个tick的时间（去除毫秒后）相同,修改为500毫秒
                    tick.datetime = tick.datetime.replace(microsecond=500)
                    tick.time = tick.datetime.strftime('%H:%M:%S.%f')

                else:
                    tick.datetime = tick.datetime.replace(microsecond=0)
                    tick.time = tick.datetime.strftime('%H:%M:%S.%f')

                dt = tick.datetime

                tick.lastPrice = float(row['LastPrice'])
                tick.volume = int(float(row['LVolume']))
                tick.bidPrice1 = float(row['BidPrice'])  # 叫买价（价格低）
                tick.bidVolume1 = int(float(row['BidVolume']))
                tick.askPrice1 = float(row['AskPrice'])  # 叫卖价（价格高）
                tick.askVolume1 = int(float(row['AskVolume']))

                # 排除涨停/跌停的数据
                if (tick.bidPrice1 == float('1.79769E308') and tick.bidVolume1 == 0) \
                        or (tick.askPrice1 == float('1.79769E308') and tick.askVolume1 == 0):
                    continue

                dtStr = tick.date + ' ' + tick.time
                if dtStr in tempTicks:
                    pass
                    # self.writeCtaError(u'日内数据重复，异常,数据时间为:{0}'.format(dtStr))
                else:
                    tempTicks[dtStr] = tick

                rawTicks.append(tick)

            del tempTicks

            # 保存到历史目录
            if len(rawTicks) > 0:
                self.__saveTicksToLocalCache(cachefilename, rawTicks)

        for t in rawTicks:
            # 推送到策略中
            self.newTick(t)

            # 保存最后一个Tick，确保savingDailyData()工作正常
            self.last_leg1_tick = t
            self.last_leg1_tick.vtSymbol = symbol

    def __loadTicksFromLocalCache(self, filename):
        """从本地缓存中，加载数据"""
        # 运行路径下cache子目录
        cacheFolder = os.getcwd() + '/cache'
        # cacheFolder = '/home/wenjiand/Workspaces/huafu-vnpy/vnpy/trader/app/ctaStrategy/strategy/cache'

        # cache文件
        cacheFile = u'{0}/{1}.pickle'. \
            format(cacheFolder, filename)

        if not os.path.isfile(cacheFile):
            return []
        else:
            # 从cache文件加载
            cache = open(cacheFile, mode='rb')
            l = cPickle.load(cache)
            cache.close()
            return l

    def __saveTicksToLocalCache(self, filename, arbticks):
        """保存价差tick到本地缓存目录"""
        # 运行路径下cache子目录
        cacheFolder = os.getcwd() + '/cache'
        # cacheFolder = '/home/wenjiand/Workspaces/huafu-vnpy/vnpy/trader/app/ctaStrategy/strategy/cache'

        # 创建cache子目录
        if not os.path.isdir(cacheFolder):
            os.mkdir(cacheFolder)

        # cache 文件名
        cacheFile = u'{0}/{1}.pickle'. \
            format(cacheFolder, filename)

        # 重复存在 返回
        if os.path.isfile(cacheFile):
            return False

        else:
            # 写入cache文件
            cache = open(cacheFile, mode='wb')
            cPickle.dump(arbticks, cache)
            cache.close()
            return True

    # ----------------------------------------------------------------------
    def runBackTestingWithArbTickFile2(self, leg1MainPath,leg2MainPath, arbSymbol):
        """运行套利回测（使用本地tick csv数据)
        参数：套利代码 SP rb1610&rb1701
        added by IncenseLee
        原始的tick，存放在相应市场下每天的目录中，目录包含市场各个合约的数据
        E:\ticks\SQ\201606\20160601\
                                     RB10.csv
                                     RB01.csv
                                     ....

        目录为交易日。
        按照回测的开始日期，到结束日期，循环每一天。

        读取eg1（如RB1610），读取Leg2（如RB701），合并成价差tick，灌输到策略的onTick中。
        """
        self.capital = self.initCapital  # 更新设置期初资金

        if len(arbSymbol) < 1:
            self.writeCtaLog(u'套利合约为空')
            return

        if not (arbSymbol.upper().index("SP") == 0 and arbSymbol.index(" ") > 0 and arbSymbol.index("&") > 0):
            self.writeCtaLog(u'套利合约格式不符合')
            return

        # 获得Leg1，leg2
        legs = arbSymbol[arbSymbol.index(" "):]
        leg1 = legs[1:legs.index("&")]
        leg2 = legs[legs.index("&") + 1:]
        self.writeCtaLog(u'Leg1:{0},Leg2:{1}'.format(leg1, leg2))

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)

            self.output(u'回测日期:{0}'.format(testday))

            # 白天数据
            self.__loadArbTicks2(leg1MainPath, leg2MainPath, testday, leg1, leg2)

    def __loadArbTicks2(self, leg1MainPath, leg2MainPath, testday, leg1Symbol, leg2Symbol):
        """加载taobao csv格式tick产生的价差合约"""

        self.writeCtaLog(u'加载回测日期:{0}\{1}的价差tick'.format(leg1MainPath, testday))
        p = re.compile(r"([A-Z]+)[0-9]+", re.I)

        leg1_shortSymbol = p.match(leg1Symbol)
        leg2_shortSymbol = p.match(leg2Symbol)

        if leg1_shortSymbol is None or leg2_shortSymbol is None:
            self.writeCtaLog(u'{0},{1}不能正则分解'.format(leg1Symbol, leg2Symbol))
            return

        leg1_shortSymbol = leg1_shortSymbol.group(1)
        leg2_shortSymbol = leg2_shortSymbol.group(1)

        arbTicks = []

        leg1File = os.path.abspath(
            os.path.join(leg1MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), testday.strftime('%Y%m%d'),
                         '{0}{1}_{2}.csv'.format(leg1_shortSymbol, leg1Symbol[-2:], testday.strftime('%Y%m%d'))))

        if not os.path.isfile(leg1File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg1File))
            return

        leg2File = os.path.abspath(
            os.path.join(leg2MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), testday.strftime('%Y%m%d'),
                         '{0}{1}_{2}.csv'.format(leg2_shortSymbol, leg2Symbol[-2:], testday.strftime('%Y%m%d'))))

        if not os.path.isfile(leg2File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg2File))
            return

        # 先读取leg2的数据到目录，以日期时间为key
        leg2Ticks = self.__loadTicksFromCsvFile(filepath=leg2File, tickDate=testday, vtSymbol=leg2Symbol)

        leg1Ticks = self.__loadTicksFromCsvFile(filepath=leg1File, tickDate=testday, vtSymbol=leg1Symbol)

        for dtStr,leg1_tick in leg1Ticks.iteritems():

            if dtStr in leg2Ticks:
                arbTick = CtaTickData()

                leg2_tick = leg2Ticks[dtStr]

                arbTick.vtSymbol = self.symbol
                arbTick.symbol = self.symbol
                arbTick.date = leg1_tick.date
                arbTick.time = leg1_tick.time
                arbTick.datetime = leg1_tick.datetime
                arbTick.tradingDay = leg1_tick.tradingDay

                arbTick.lastPrice = EMPTY_FLOAT
                arbTick.volume = EMPTY_INT

                # 排除涨停/跌停的数据
                if ((leg1_tick.askPrice1 == float('1.79769E308') or leg1_tick.askPrice1 == 0) and leg1_tick.askVolume1 == 0) \
                        or ((leg1_tick.bidPrice1 == float('1.79769E308') or leg1_tick.bidPrice1 == 0) and leg1_tick.bidVolume1 == 0):
                    continue

                if ((leg2_tick.askPrice1 == float('1.79769E308') or leg2_tick.askPrice1 == 0) and leg2_tick.askVolume1 == 0) \
                        or ((leg2_tick.bidPrice1 == float('1.79769E308') or leg2_tick.bidPrice1 == 0) and leg2_tick.bidVolume1 == 0):
                    continue

                # 叫卖价差=leg1.askPrice1 - leg2.bidPrice1，volume为两者最小
                arbTick.askPrice1 = leg1_tick.askPrice1 - leg2_tick.bidPrice1
                arbTick.askVolume1 = min(leg1_tick.askVolume1, leg2_tick.bidVolume1)

                # 叫买价差=leg1.bidPrice1 - leg2.askPrice1，volume为两者最小
                arbTick.bidPrice1 = leg1_tick.bidPrice1 - leg2_tick.askPrice1
                arbTick.bidVolume1 = min(leg1_tick.bidVolume1, leg2_tick.askVolume1)

                arbTicks.append(arbTick)

                del leg2Ticks[dtStr]

        for t in arbTicks:
            # 推送到策略中
            self.newTick(t)

    def runBackTestingWithNonStrArbTickFile(self, leg1MainPath, leg2MainPath, leg1Symbol,leg2Symbol):
        """运行套利回测（使用本地tick txt数据)
        参数：
        leg1MainPath： leg1合约所在的市场路径
        leg2MainPath： leg2合约所在的市场路径
        leg1Symbol： leg1合约
        Leg2Symbol：leg2合约
        added by IncenseLee
        原始的tick，分别存放在白天目录1和夜盘目录2中，每天都有各个合约的数据
        Z:\ticks\SHFE\201606\RB\0601\
                                     RB1610.txt
                                     RB1701.txt
                                     ....
        Z:\ticks\SHFE_night\201606\RB\0601
                                     RB1610.txt
                                     RB1701.txt
                                     ....

        夜盘目录为自然日，不是交易日。

        按照回测的开始日期，到结束日期，循环每一天。
        每天优先读取日盘数据，再读取夜盘数据。
        读取eg1（如RB1610），读取Leg2（如RB701），根据两者tick的时间优先顺序，逐一tick灌输到策略的onTick中。
        """
        self.capital = self.initCapital  # 更新设置期初资金

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol)<1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        #首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days = i)

            self.output(u'回测日期:{0}'.format(testday))
            # 撤销所有之前的orders
            if self.symbol:
                self.cancelOrders(self.symbol)
            if self.last_leg1_tick:
                self.cancelOrders(self.last_leg1_tick.vtSymbol)
            if self.last_leg2_tick:
                self.cancelOrders(self.last_leg2_tick.vtSymbol)
            # 加载运行白天数据
            self.__loadNotStdArbTicks(leg1MainPath, leg2MainPath, testday, leg1Symbol,leg2Symbol)

            self.savingDailyData(testday, self.capital, self.maxCapital,self.totalCommission)

            # 加载运行夜盘数据
            self.__loadNotStdArbTicks(leg1MainPath+'_night', leg2MainPath+'_night', testday, leg1Symbol, leg2Symbol)

        self.savingDailyData(self.dataEndDate, self.capital, self.maxCapital,self.totalCommission)

    def __loadTicksFromTxtFile(self, filepath, tickDate, vtSymbol):
        """从文件中读取tick"""
        # 先读取数据到Dict，以日期时间为key
        ticks = OrderedDict()

        if not os.path.isfile(filepath):
            self.writeCtaLog(u'{0}文件不存在'.format(filepath))
            return ticks
        dt = None
        csvReadFile = open(filepath, 'rb')

        reader = csv.DictReader(csvReadFile, delimiter=",")
        self.writeCtaLog(u'加载{0}'.format(filepath))
        for row in reader:
            tick = CtaTickData()

            tick.vtSymbol = vtSymbol
            tick.symbol = vtSymbol

            tick.date = tickDate.strftime('%Y%m%d')
            tick.tradingDay = tick.date
            tick.time = row['Time']

            try:
                tick.datetime = datetime.strptime(tick.date + ' ' + tick.time, '%Y%m%d %H:%M:%S.%f')
            except Exception as ex:
                self.writeCtaError(u'日期转换错误:{0},{1}:{2}'.format(tick.date + ' ' + tick.time, Exception, ex))
                continue

            # 修正毫秒
            if tick.datetime.replace(microsecond=0) == dt:
                # 与上一个tick的时间（去除毫秒后）相同,修改为500毫秒
                tick.datetime = tick.datetime.replace(microsecond=500)
                tick.time = tick.datetime.strftime('%H:%M:%S.%f')

            else:
                tick.datetime = tick.datetime.replace(microsecond=0)
                tick.time = tick.datetime.strftime('%H:%M:%S.%f')

            dt = tick.datetime

            tick.lastPrice = float(row['LastPrice'])
            tick.volume = int(float(row['LVolume']))
            tick.bidPrice1 = float(row['BidPrice'])  # 叫买价（价格低）
            tick.bidVolume1 = int(float(row['BidVolume']))
            tick.askPrice1 = float(row['AskPrice'])  # 叫卖价（价格高）
            tick.askVolume1 = int(float(row['AskVolume']))

            # 排除涨停/跌停的数据
            if (tick.bidPrice1 == float('1.79769E308') and tick.bidVolume1 == 0 and tick.askVolume1 >0) \
                    or (tick.askPrice1 == float('1.79769E308') and tick.askVolume1 == 0 and tick.bidVolume1>0):
                continue

            dtStr = tick.date + ' ' + tick.time
            if dtStr in ticks:
                pass
                #self.writeCtaError(u'日内数据重复，异常,数据时间为:{0}'.format(dtStr))
            else:
                ticks[dtStr] = tick

        return ticks

    def __loadNotStdArbTicks(self, leg1MainPath,leg2MainPath, testday, leg1Symbol, leg2Symbol):

        self.writeCtaLog(u'加载回测日期:{0}的价差tick'.format( testday))
        p = re.compile(r"([A-Z]+)[0-9]+", re.I)

        leg1_shortSymbol = p.match(leg1Symbol)
        leg2_shortSymbol = p.match(leg2Symbol)

        if leg1_shortSymbol is None or leg2_shortSymbol is None:
            self.writeCtaLog(u'{0},{1}不能正则分解'.format(leg1Symbol, leg2Symbol))
            return

        leg1_shortSymbol = leg1_shortSymbol.group(1)
        leg2_shortSymbol = leg2_shortSymbol.group(1)

        # E:\Ticks\ZJ\2015\201505\TF
        leg1File = os.path.abspath(os.path.join(leg1MainPath, testday.strftime('%Y'),testday.strftime('%Y%m'),leg1_shortSymbol,testday.strftime('%m%d'),'{0}.txt'.format(leg1Symbol)))
        #leg1File = u'{0}\\{1}\\{2}\\{3}\\{4}\\{5}.txt' \
        #    .format(leg1MainPath, testday.strftime('%Y'),testday.strftime('%Y%m'), leg1_shortSymbol, testday.strftime('%m%d'), leg1Symbol)
        if not os.path.isfile(leg1File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg1File))
            return

        leg2File=os.path.abspath(os.path.join(leg2MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), leg2_shortSymbol,
                                     testday.strftime('%m%d'), '{0}.txt'.format(leg2Symbol)))
        #leg2File = u'{0}\\{1}\\{2}\\{3}\\{4}\\{5}.txt' \
        #    .format(leg2MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), leg2_shortSymbol, testday.strftime('%m%d'), leg2Symbol)
        if not os.path.isfile(leg2File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg2File))
            return

        leg1Ticks = self.__loadTicksFromTxtFile(filepath=leg1File, tickDate= testday, vtSymbol=leg1Symbol)
        if len(leg1Ticks) == 0:
            self.writeCtaLog(u'{0}读取tick数为空'.format(leg1File))
            return

        leg2Ticks = self.__loadTicksFromTxtFile(filepath=leg2File, tickDate=testday, vtSymbol=leg2Symbol)
        if len(leg2Ticks) == 0:
            self.writeCtaLog(u'{0}读取tick数为空'.format(leg1File))
            return

        leg1_tick = None
        leg2_tick = None

        while not (len(leg1Ticks) == 0 or len(leg2Ticks) == 0):
            if leg1_tick is None and len(leg1Ticks) > 0:
                leg1_tick = leg1Ticks.popitem(last=False)
            if leg2_tick is None and len(leg2Ticks) > 0:
                leg2_tick = leg2Ticks.popitem(last=False)

            if leg1_tick is None and leg2_tick is not None:
                self.newTick(leg2_tick[1])
                self.last_leg2_tick = leg2_tick[1]
                leg2_tick = None
            elif leg1_tick is not None and leg2_tick is None:
                self.newTick(leg1_tick[1])
                self.last_leg1_tick = leg1_tick[1]
                leg1_tick = None
            elif leg1_tick is not None and leg2_tick is not None:
                leg1 = leg1_tick[1]
                leg2 = leg2_tick[1]
                self.last_leg2_tick = leg2_tick[1]
                self.last_leg1_tick = leg1_tick[1]
                if leg1.datetime <= leg2.datetime:
                    self.newTick(leg1)
                    leg1_tick = None
                else:
                    self.newTick(leg2)
                    leg2_tick = None

    def runBackTestingWithNonStrArbTickFile2(self, leg1MainPath, leg2MainPath, leg1Symbol, leg2Symbol):
        """运行套利回测（使用本地tickcsv数据，数据从taobao标普购买)
        参数：
        leg1MainPath： leg1合约所在的市场路径
        leg2MainPath： leg2合约所在的市场路径
        leg1Symbol： leg1合约
        Leg2Symbol：leg2合约
        added by IncenseLee
        原始的tick，存放在相应市场下每天的目录中，目录包含市场各个合约的数据
        E:\ticks\SQ\201606\20160601\
                                     RB10.csv
                                     RB01.csv
                                     ....

        目录为交易日。
        按照回测的开始日期，到结束日期，循环每一天。

        读取eg1（如RB1610），读取Leg2（如RB701），根据两者tick的时间优先顺序，逐一tick灌输到策略的onTick中。
        """
        self.capital = self.initCapital  # 更新设置期初资金

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)

            self.output(u'回测日期:{0}'.format(testday))
            # 撤销所有之前的orders
            if self.symbol:
                self.cancelOrders(self.symbol)
            if self.last_leg1_tick:
                self.cancelOrders(self.last_leg1_tick.vtSymbol)
            if self.last_leg2_tick:
                self.cancelOrders(self.last_leg2_tick.vtSymbol)

            # 加载运行每天数据
            self.__loadNotStdArbTicks2(leg1MainPath, leg2MainPath, testday, leg1Symbol, leg2Symbol)

            self.savingDailyData(testday, self.capital, self.maxCapital,self.totalCommission)


    def __loadTicksFromCsvFile(self, filepath, tickDate, vtSymbol):
        """从csv文件中UnicodeDictReader读取tick"""
        # 先读取数据到Dict，以日期时间为key
        ticks = OrderedDict()

        if not os.path.isfile(filepath):
            self.writeCtaLog(u'{0}文件不存在'.format(filepath))
            return ticks
        dt = None
        csvReadFile = open(filepath, 'rb')
        df = pd.read_csv(filepath, encoding='gbk',parse_dates=False)
        df.columns = ['date', 'time', 'lastPrice', 'lastVolume', 'totalInterest', 'position',
                      'bidPrice1', 'bidVolume1', 'bidPrice2', 'bidVolume2', 'bidPrice3', 'bidVolume3',
                      'askPrice1', 'askVolume1', 'askPrice2', 'askVolume2', 'askPrice3', 'askVolume3','BS']
        self.writeCtaLog(u'加载{0}'.format(filepath))
        for i in range(0,len(df)):
            #日期, 时间, 成交价, 成交量, 总量, 属性(持仓增减), B1价, B1量, B2价, B2量, B3价, B3量, S1价, S1量, S2价, S2量, S3价, S3量, BS
            # 0    1      2      3       4      5               6     7    8     9     10     11    12    13    14   15    16   17    18
            row = df.iloc[i].to_dict()

            tick = CtaTickData()

            tick.vtSymbol = vtSymbol
            tick.symbol = vtSymbol

            tick.date = row['date']
            tick.tradingDay = tickDate.strftime('%Y%m%d')
            tick.time = row['time']

            try:
                tick.datetime = datetime.strptime(tick.date + ' ' + tick.time, '%Y-%m-%d %H:%M:%S')
            except Exception as ex:
                self.writeCtaError(u'日期转换错误:{0},{1}:{2}'.format(tick.date + ' ' + tick.time, Exception, ex))
                continue

            tick.date = tick.datetime.strftime('%Y%m%d')
            # 修正毫秒
            if tick.datetime.replace(microsecond=0) == dt:
                # 与上一个tick的时间（去除毫秒后）相同,修改为500毫秒
                tick.datetime = tick.datetime.replace(microsecond=500)
                tick.time = tick.datetime.strftime('%H:%M:%S.%f')

            else:
                tick.datetime = tick.datetime.replace(microsecond=0)
                tick.time = tick.datetime.strftime('%H:%M:%S.%f')

            dt = tick.datetime

            tick.lastPrice = float(row['lastPrice'])
            tick.volume = int(float(row['lastVolume']))
            tick.bidPrice1 = float(row['bidPrice1'])  # 叫买价（价格低）
            tick.bidVolume1 = int(float(row['bidVolume1']))
            tick.askPrice1 = float(row['askPrice1'])  # 叫卖价（价格高）
            tick.askVolume1 = int(float(row['askVolume1']))

            # 排除涨停/跌停的数据
            if (tick.bidPrice1 == float('1.79769E308') and tick.bidVolume1 == 0) \
                    or (tick.askPrice1 == float('1.79769E308') and tick.askVolume1 == 0):
                continue

            dtStr = tick.date + ' ' + tick.time
            if dtStr in ticks:
                pass

                #self.writeCtaError(u'日内数据重复，异常,数据时间为:{0}'.format(dtStr))
            else:
                ticks[dtStr] = tick
        # todo release memory
        # lst = [df]
        # del df
        # del lst

        return ticks

    def __loadNotStdArbTicks2(self, leg1MainPath, leg2MainPath, testday,  leg1Symbol, leg2Symbol):

        self.writeCtaLog(u'加载回测日期:{0}的价差tick'.format(testday))
        p = re.compile(r"([A-Z]+)[0-9]+",re.I)
        leg1_shortSymbol = p.match(leg1Symbol)
        leg2_shortSymbol = p.match(leg2Symbol)

        if leg1_shortSymbol is None or leg2_shortSymbol is None:
            self.writeCtaLog(u'{0},{1}不能正则分解'.format(leg1Symbol, leg2Symbol))
            return

        leg1_shortSymbol = leg1_shortSymbol.group(1)
        leg2_shortSymbol = leg2_shortSymbol.group(1)


        # E:\Ticks\SQ\2014\201401\20140102\ag01_20140102.csv
        #leg1File = u'e:\\ticks\\{0}\\{1}\\{2}\\{3}\\{4}{5}_{3}.csv' \
        #    .format(leg1MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), testday.strftime('%Y%m%d'), leg1_shortSymbol, leg1Symbol[-2:])

        leg1File = os.path.abspath(
            os.path.join(leg1MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), testday.strftime('%Y%m%d'),
                         '{0}{1}_{2}.csv'.format(leg1_shortSymbol,leg1Symbol[-2:],testday.strftime('%Y%m%d'))))

        if not os.path.isfile(leg1File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg1File))
            return

        #leg2File = u'e:\\ticks\\{0}\\{1}\\{2}\\{3}\\{4}{5}_{3}.csv' \
        #    .format(leg2MainPath,testday.strftime('%Y'), testday.strftime('%Y%m'),  testday.strftime('%Y%m%d'), leg2_shortSymbol, leg2Symbol[-2:])
        leg2File = os.path.abspath(
            os.path.join(leg1MainPath, testday.strftime('%Y'), testday.strftime('%Y%m'), testday.strftime('%Y%m%d'),
                         '{0}{1}_{2}.csv'.format(leg2_shortSymbol, leg2Symbol[-2:], testday.strftime('%Y%m%d'))))

        if not os.path.isfile(leg2File):
            self.writeCtaLog(u'{0}文件不存在'.format(leg2File))
            return

        leg1Ticks = self.__loadTicksFromCsvFile(filepath=leg1File, tickDate=testday, vtSymbol=leg1Symbol)
        if len(leg1Ticks) == 0:
            self.writeCtaLog(u'{0}读取tick数为空'.format(leg1File))
            return

        leg2Ticks = self.__loadTicksFromCsvFile(filepath=leg2File, tickDate=testday, vtSymbol=leg2Symbol)
        if len(leg2Ticks) == 0:
            self.writeCtaLog(u'{0}读取tick数为空'.format(leg1File))
            return

        leg1_tick = None
        leg2_tick = None

        while not (len(leg1Ticks) == 0 or len(leg2Ticks) == 0):
            if leg1_tick is None and len(leg1Ticks) > 0:
                leg1_tick = leg1Ticks.popitem(last=False)
            if leg2_tick is None and len(leg2Ticks) > 0:
                leg2_tick = leg2Ticks.popitem(last=False)

            if leg1_tick is None and leg2_tick is not None:
                self.newTick(leg2_tick[1])
                self.last_leg2_tick = leg2_tick[1]
                leg2_tick = None
            elif leg1_tick is not None and leg2_tick is None:
                self.newTick(leg1_tick[1])
                self.last_leg1_tick = leg1_tick[1]
                leg1_tick = None
            elif leg1_tick is not None and leg2_tick is not None:
                leg1 = leg1_tick[1]
                leg2 = leg2_tick[1]
                self.last_leg1_tick = leg1_tick[1]
                self.last_leg2_tick = leg2_tick[1]
                if leg1.datetime <= leg2.datetime:
                    self.newTick(leg1)
                    leg1_tick = None
                else:
                    self.newTick(leg2)
                    leg2_tick = None

    def runBackTestingWithNonStrArbTickFromMongoDB(self, leg1Symbol, leg2Symbol):
        """运行套利回测（使用服务器数据，数据从taobao标普购买)
        参数：        
        leg1Symbol： leg1合约
        Leg2Symbol：leg2合约
        added by IncenseLee
        
        目录为交易日。
        按照回测的开始日期，到结束日期，循环每一天。

        读取eg1（如RB1610），读取Leg2（如RB1701），根据两者tick的时间优先顺序，逐一tick灌输到策略的onTick中。
        """

        # 连接数据库
        host, port, log = loadMongoSetting()
        self.dbClient = pymongo.MongoClient(host, port)

        self.capital = self.initCapital  # 更新设置期初资金

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return
        # RB
        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            self.writeCtaLog(u'本回测仅支持tick模式')
            return

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)

            self.output(u'回测日期:{0}'.format(testday))

            # 撤销所有之前的orders
            if self.symbol:
                self.cancelOrders(self.symbol)
            if self.last_leg1_tick:
                self.cancelOrders(self.last_leg1_tick.vtSymbol)
            if self.last_leg2_tick:
                self.cancelOrders(self.last_leg2_tick.vtSymbol)
            # 加载运行每天数据
            self.__loadNotStdArbTicksFromMongoDB( testday, leg1Symbol, leg2Symbol)

            self.savingDailyData(testday, self.capital, self.maxCapital,self.totalCommission)

    def __loadNotStdArbTicksFromMongoDB(self,testday, leg1Symbol, leg2Symbol):
        self.writeCtaLog(u'从MongoDB加载回测日期:{0}的{1}-{2}价差tick'.format(testday,leg1Symbol, leg2Symbol))

        leg1Ticks = self.__loadTicksFromMongoDB(tickDate=testday, vtSymbol=leg1Symbol)
        if len(leg1Ticks) == 0:
            self.writeCtaLog(u'读取{0}tick数为空'.format(leg1Symbol))
            return

        leg2Ticks = self.__loadTicksFromMongoDB(tickDate=testday, vtSymbol=leg2Symbol)
        if len(leg2Ticks) == 0:
            self.writeCtaLog(u'读取{0}tick数为空'.format(leg1Symbol))
            return

        leg1_tick = None
        leg2_tick = None

        while not (len(leg1Ticks) == 0 or len(leg2Ticks) == 0):
            if leg1_tick is None and len(leg1Ticks) > 0:
                leg1_tick = leg1Ticks.popitem(last=False)
            if leg2_tick is None and len(leg2Ticks) > 0:
                leg2_tick = leg2Ticks.popitem(last=False)

            if leg1_tick is None and leg2_tick is not None:
                self.newTick(leg2_tick[1])
                self.last_leg2_tick = leg2_tick[1]
                leg2_tick = None
            elif leg1_tick is not None and leg2_tick is None:
                self.newTick(leg1_tick[1])
                self.last_leg1_tick = leg1_tick[1]
                leg1_tick = None
            elif leg1_tick is not None and leg2_tick is not None:
                leg1 = leg1_tick[1]
                leg2 = leg2_tick[1]
                self.last_leg1_tick = leg1_tick[1]
                self.last_leg2_tick = leg2_tick[1]
                if leg1.datetime <= leg2.datetime:
                    self.newTick(leg1)
                    leg1_tick = None
                else:
                    self.newTick(leg2)
                    leg2_tick = None

    def __loadTicksFromMongoDB(self, tickDate, vtSymbol):
        """从mongodb读取tick"""
        # 先读取数据到Dict，以日期时间为key
        ticks = OrderedDict()

        p = re.compile(r"([A-Z]+)[0-9]+", re.I)
        shortSymbol = p.match(vtSymbol)
        if shortSymbol is None :
            self.writeCtaLog(u'{0}不能正则分解'.format(vtSymbol))
            return

        shortSymbol = shortSymbol.group(1)
        shortSymbol = shortSymbol + vtSymbol[-2:]               # 例如 AU01

        testday_monrning = tickDate.replace(hour=0, minute=0, second=0, microsecond=0)
        testday_midnight = tickDate.replace(hour=23, minute=59, second=59, microsecond=999999)

        # 载入初始化需要用的数据
        flt = {'datetime': {'$gte': testday_monrning,
                            '$lt': testday_midnight}}
        db = self.dbClient[self.dbName]
        collection = db[shortSymbol]
        initCursor = collection.find(flt).sort('datetime', pymongo.ASCENDING)

        # 将数据从查询指针中读取出，并生成列表
        count_ticks = 0
        for d in initCursor:
            tick = CtaTickData()
            tick.__dict__ = d
            # 更新symbol
            tick.vtSymbol = vtSymbol
            tick.symbol = vtSymbol

            # 排除涨停/跌停的数据
            if (tick.bidPrice1 == float('1.79769E308') and tick.bidVolume1 == 0) \
                    or (tick.askPrice1 == float('1.79769E308') and tick.askVolume1 == 0):
                continue
            dtStr = tick.date + ' ' + tick.time
            if dtStr not in ticks:
                ticks[dtStr] = tick

            count_ticks += 1

        return ticks

    #----------------------------------------------------------------------
    def runBackTestingWithBarFile(self, filename):
        """运行回测（使用本地csv数据)
        added by IncenseLee
        """
        self.capital = self.initCapital      # 更新设置期初资金
        if not filename:
            self.writeCtaLog(u'请指定回测数据文件')
            return

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        import os
        if not os.path.isfile(filename):
            self.writeCtaLog(u'{0}文件不存在'.format(filename))

        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        # 首先根据回测模式，确认要使用的数据类
        if not self.mode == self.BAR_MODE:
            self.writeCtaLog(u'文件仅支持bar模式，若扩展tick模式，需要修改本方法')
            return

        #self.output(u'开始回测')

        #self.strategy.inited = True
        self.strategy.onInit()
        self.output(u'策略初始化完成')

        self.output(u'开始回放数据')

        import csv
        csvfile = open(filename,'r',encoding='utf8')
        reader = csv.DictReader((line.replace('\0', '') for line in csvfile), delimiter=",")
        last_tradingDay = None
        for row in reader:
            try:
                bar = CtaBarData()
                bar.symbol = self.symbol
                bar.vtSymbol = self.symbol

                # 从tb导出的csv文件
                #bar.open = float(row['Open'])
                #bar.high = float(row['High'])
                #bar.low = float(row['Low'])
                #bar.close = float(row['Close'])
                #bar.volume = float(row['TotalVolume'])#
                #barEndTime = datetime.strptime(row['Date']+' ' + row['Time'], '%Y/%m/%d %H:%M:%S')
                if row.get('open',None) is None:
                    continue
                if row.get('high', None) is None:
                    continue
                if row.get('low', None) is None:
                    continue
                if row.get('close', None) is None:
                    continue

                if len(row['open'])==0 or len(row['high'])==0 or len(row['low'])==0 or len(row['close'])==0:
                    continue
                # 从ricequant导出的csv文件
                bar.open = self.roundToPriceTick(float(row['open']))
                bar.high = self.roundToPriceTick(float(row['high']))
                bar.low = self.roundToPriceTick(float(row['low']))
                bar.close = self.roundToPriceTick(float(row['close']))
                bar.volume = float(row['volume']) if len(row['volume'])>0 else 0
                if '-' in row['index']:
                    barEndTime = datetime.strptime(row['index'], '%Y-%m-%d %H:%M:%S')
                else:
                    barEndTime = datetime.strptime(row['datetime'], '%Y%m%d%H%M%S')

                # 使用Bar的开始时间作为datetime
                bar.datetime = barEndTime - timedelta(seconds=self.barTimeInterval)

                bar.date = bar.datetime.strftime('%Y-%m-%d')
                bar.time = bar.datetime.strftime('%H:%M:%S')
                if 'trading_date' in row:
                    if len(row['trading_date']) is 8:
                        bar.tradingDay = row['trading_date'][0:4] + '-' + row['trading_date'][4:6] + '-' + row['trading_date'][6:]
                    else:
                        bar.tradingDay = row['trading_date']
                else:
                    if bar.datetime.hour >=21 and not self.is_7x24:
                        if bar.datetime.isoweekday() == 5:
                            # 星期五=》星期一
                            bar.tradingDay = (barEndTime + timedelta(days=3)).strftime('%Y-%m-%d')
                        else:
                            # 第二天
                            bar.tradingDay = (barEndTime + timedelta(days=1)).strftime('%Y-%m-%d')
                    elif bar.datetime.hour < 8 and bar.datetime.isoweekday() == 6 and not self.is_7x24:
                        # 星期六=>星期一
                        bar.tradingDay = (barEndTime + timedelta(days=2)).strftime('%Y-%m-%d')
                    else:
                        bar.tradingDay = bar.date

                if not (bar.datetime < self.dataStartDate or bar.datetime >= self.dataEndDate):
                    if last_tradingDay != bar.tradingDay:
                        if last_tradingDay is not None:
                            self.savingDailyData(datetime.strptime(last_tradingDay, '%Y-%m-%d'), self.capital,
                                                 self.maxCapital,self.totalCommission,benchmark=bar.close)
                        last_tradingDay = bar.tradingDay

                    # Simulate latest tick and send it to Strategy
                    simTick = self.__barToTick(bar)
                    # self.tick = simTick
                    self.strategy.curTick = simTick

                    # Check the order triggers and deliver the bar to the Strategy
                    if self.useBreakoutMode is False:
                        self.newBar(bar)
                    else:
                        self.newBarForBreakout(bar)

                if not self.strategy.trading and self.strategyStartDate < bar.datetime:
                    self.strategy.trading = True
                    self.strategy.onStart()
                    self.output(u'策略启动完成')

                if self.netCapital < 0:
                    self.writeCtaError(u'净值低于0，回测停止')
                    return

            except Exception as ex:
                self.writeCtaError(u'回测异常导致停止')
                self.writeCtaError(u'{},{}'.format(str(ex),traceback.format_exc()))
                return

    #----------------------------------------------------------------------
    def runBackTestingWithDataSource(self):
        """运行回测（使用本地csv数据)
        added by IncenseLee
        """
        self.capital = self.initCapital  # 更新设置期初资金
        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        if len(self.symbol) < 1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        # 首先根据回测模式，确认要使用的数据类
        if not self.mode == self.BAR_MODE:
            self.writeCtaLog(u'文件仅支持bar模式，若扩展tick模式，需要修改本方法')
            return

        # self.output(u'开始回测')

        # self.strategy.inited = True
        self.strategy.onInit()
        self.output(u'策略初始化完成')

        self.strategy.trading = True
        self.strategy.onStart()
        self.output(u'策略启动完成')

        self.output(u'开始载入数据')

        # 载入回测数据
        testdays = (self.dataEndDate - self.dataStartDate).days

        rawBars = []
        # 看本地缓存是否存在
        cachefilename = u'{0}_{1}_{2}'.format(self.symbol, self.dataStartDate.strftime('%Y%m%d'),
                                              self.dataEndDate.strftime('%Y%m%d'))
        rawBars = self.__loadTicksFromLocalCache(cachefilename)

        if len(rawBars) < 1:
            self.writeCtaLog(u'从数据库中读取数据')

            query_time = datetime.now()
            ds = DataSource()
            start_date = self.dataStartDate.strftime('%Y-%m-%d')
            end_date = self.dataEndDate.strftime('%Y-%m-%d')
            fields = ['open', 'close', 'high', 'low', 'volume', 'open_interest', 'limit_up', 'limit_down',
                      'trading_date']
            last_bar_dt = None

            df = ds.get_price(order_book_id=self.strategy.symbol, start_date=start_date,
                              end_date=end_date, frequency='1m', fields=fields)

            process_time = datetime.now()
            # 将数据从查询指针中读取出，并生成列表
            count_bars = 0
            self.writeCtaLog(u'一共获取{}条{}分钟数据'.format(len(df), '1m'))
            for idx in df.index:
                row = df.loc[idx]
                # self.writeCtaLog('{}: {}, o={}, h={}, l={}, c={}'.format(count_bars, datetime.strptime(str(idx), '%Y-%m-%d %H:%M:00'),
                #                                                          row['open'], row['high'], row['low'], row['close']))
                bar = CtaBarData()
                bar.vtSymbol = self.symbol
                bar.symbol = self.symbol
                last_bar_dt = datetime.strptime(str(idx), '%Y-%m-%d %H:%M:00')
                bar.datetime = last_bar_dt - timedelta(minutes=1)
                bar.date = bar.datetime.strftime('%Y-%m-%d')
                bar.time = bar.datetime.strftime('%H:%M:00')
                bar.tradingDay = datetime.strptime(str(int(row['trading_date'])), '%Y%m%d')
                bar.open = float(row['open'])
                bar.high = float(row['high'])
                bar.low = float(row['low'])
                bar.close = float(row['close'])
                bar.volume = int(row['volume'])
                rawBars.append(bar)
                count_bars += 1

            self.writeCtaLog(u'回测日期{}-{}，数据量：{}，查询耗时:{},回测耗时:{}'
                             .format(self.dataStartDate.strftime('%Y-%m-%d'), self.dataEndDate.strftime('%Y%m%d'),
                                     count_bars, str(datetime.now() - query_time), str(datetime.now() - process_time)))

            # 保存本地cache文件
            if count_bars > 0:
                self.__saveTicksToLocalCache(cachefilename, rawBars)

        if len(rawBars) < 1:
            self.writeCtaLog(u'ERROR 拿不到指定日期的数据，结束')
            return

        self.output(u'开始回放数据')
        last_tradingDay = 0
        for bar in rawBars:
            # self.writeCtaLog(u'{} o:{};h:{};l:{};c:{},v:{},tradingDay:{},H2_count:{}'
            #                 .format(bar.date+' '+bar.time, bar.open, bar.high,
            #                         bar.low, bar.close, bar.volume, bar.tradingDay, self.lineH2.m1_bars_count))

            # if not (bar.datetime < self.dataStartDate or bar.datetime >= self.dataEndDate):
            if True:
                if last_tradingDay == 0:
                    last_tradingDay = bar.tradingDay
                elif last_tradingDay != bar.tradingDay:
                    if last_tradingDay is not None:
                        self.savingDailyData(last_tradingDay, self.capital, self.maxCapital, self.totalCommission)
                    last_tradingDay = bar.tradingDay

                # Simulate latest tick and send it to Strategy
                simTick = self.__barToTick(bar)
                # self.tick = simTick
                self.strategy.curTick = simTick

                # Check the order triggers and deliver the bar to the Strategy
                if self.useBreakoutMode is False:
                    self.newBar(bar)
                else:
                    self.newBarForBreakout(bar)

            if self.netCapital < 0:
                self.writeCtaError(u'净值低于0，回测停止')
                return

    #----------------------------------------------------------------------
    def runBacktestingWithMysql(self):
        """运行回测(使用Mysql数据）
        added by IncenseLee
        """
        self.capital = self.initCapital      # 更新设置期初资金

        if not self.dataStartDate:
            self.writeCtaLog(u'回测开始日期未设置。')
            return

        if not self.dataEndDate:
            self.dataEndDate = datetime.today()

        if len(self.symbol)<1:
            self.writeCtaLog(u'回测对象未设置。')
            return

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            dataClass = CtaBarData
            func = self.newBar
        else:
            dataClass = CtaTickData
            func = self.newTick

        self.output(u'开始回测')

        #self.strategy.inited = True
        self.strategy.onInit()
        self.output(u'策略初始化完成')

        self.strategy.trading = True
        self.strategy.onStart()
        self.output(u'策略启动完成')

        self.output(u'开始回放数据')

        # 每次获取日期周期
        intervalDays = 10

        for i in range (0,(self.dataEndDate - self.dataStartDate).days +1, intervalDays):
            d1 = self.dataStartDate + timedelta(days = i )

            if (self.dataEndDate - d1).days > intervalDays:
                d2 = self.dataStartDate + timedelta(days = i + intervalDays -1 )
            else:
                d2 = self.dataEndDate

            # 提取历史数据
            self.loadDataHistoryFromMysql(self.symbol, d1, d2)

            self.output(u'数据日期:{0} => {1}'.format(d1,d2))
            # 将逐笔数据推送
            for data in self.historyData:

                # 记录最新的TICK数据
                self.tick = self.__dataToTick(data)
                self.dt = self.tick.datetime

                # 处理限价单
                self.crossLimitOrder()
                self.crossStopOrder()

                # 推送到策略引擎中
                self.strategy.onTick(self.tick)

            # 清空历史数据
            self.historyData = []

        self.output(u'数据回放结束')

    #----------------------------------------------------------------------
    def runBacktesting(self):
        """运行回测"""

        self.capital = self.initCapital      # 更新设置期初资金

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            dataClass = CtaBarData
            func = self.newBar
        else:
            dataClass = CtaTickData
            func = self.newTick

        self.output(u'开始回测')
        
        self.strategy.inited = True
        self.strategy.onInit()
        self.output(u'策略初始化完成')
        
        self.strategy.trading = True
        self.strategy.onStart()
        self.output(u'策略启动完成')
        
        self.output(u'开始回放数据')

        # 循环加载回放数据
        self.runHistoryDataFromMongo()

            
        self.output(u'数据回放结束')

    # ----------------------------------------------------------------------
    def runHistoryDataFromMongo(self):
        """
        根据测试的每一天，从MongoDB载入历史数据，并推送Tick至回测函数
        :return: 
        """

        host, port, log = loadMongoSetting()

        self.dbClient = pymongo.MongoClient(host, port)
        collection = self.dbClient[self.dbName][self.symbol]

        self.output(u'开始载入数据')

        # 首先根据回测模式，确认要使用的数据类
        if self.mode == self.BAR_MODE:
            dataClass = CtaBarData
            func = self.newBar
        else:
            dataClass = CtaTickData
            func = self.newTick

        # 载入回测数据
        if not self.dataEndDate:
            self.dataEndDate = datetime.now()

        testdays = (self.dataEndDate - self.dataStartDate).days

        if testdays < 1:
            self.writeCtaLog(u'回测时间不足')
            return

        # 循环每一天
        for i in range(0, testdays):
            testday = self.dataStartDate + timedelta(days=i)
            testday_monrning = testday  #testday.replace(hour=0, minute=0, second=0, microsecond=0)
            testday_midnight = testday + timedelta(days=1) #testday.replace(hour=23, minute=59, second=59, microsecond=999999)

            query_time = datetime.now()
            # 载入初始化需要用的数据
            flt = {'datetime': {'$gte': testday_monrning,
                                '$lt': testday_midnight}}

            initCursor = collection.find(flt).sort('datetime', pymongo.ASCENDING)

            process_time = datetime.now()
            # 将数据从查询指针中读取出，并生成列表
            count_ticks = 0

            for d in initCursor:
                data = dataClass()
                data.__dict__ = d
                func(data)
                count_ticks += 1

            self.output(u'回测日期{0}，数据量：{1}，查询耗时:{2},回测耗时:{3}'
                        .format(testday.strftime('%Y-%m-%d'), count_ticks, str(datetime.now() - query_time),
                                str(datetime.now() - process_time)))
            # 记录每日净值
            self.savingDailyData(testday, self.capital, self.maxCapital,self.totalCommission)

    def __sendOnBarEvent(self, bar):
        """发送Bar的事件"""
        if self.eventEngine is not None:
            eventType = EVENT_ON_BAR + '_' + self.symbol
            event = Event(type_= eventType)
            event.dict_['data'] = bar
            self.eventEngine.put(event)

    # ----------------------------------------------------------------------
    def newBar(self, bar):
        """新的K线"""
        self.bar = bar
        self.dt = bar.datetime
        self.crossLimitOrder()      # 先撮合限价单
        self.crossStopOrder()       # 再撮合停止单
        self.strategy.onBar(bar)    # 推送K线到策略中
        self.__sendOnBarEvent(bar)  # 推送K线到事件
        self.last_bar = bar

    # ----------------------------------------------------------------------
    def newBarForBreakout(self, bar):
        """新的K线"""
        self.bar = bar
        self.dt = bar.datetime
        self.strategy.onBar(bar)    # 推送K线到策略中
        self.crossLimitOrder()      # 先撮合限价单
        self.crossStopOrder()       # 再撮合停止单
        self.__sendOnBarEvent(bar)  # 推送K线到事件
        self.last_bar = bar

    # ----------------------------------------------------------------------
    def newTick(self, tick):
        """新的Tick"""
        self.tick = tick
        self.dt = tick.datetime
        self.crossLimitOrder()
        self.crossStopOrder()
        self.strategy.onTick(tick)

    # ----------------------------------------------------------------------
    def initStrategy(self, strategyClass, setting=None):
        """
        初始化策略
        setting是策略的参数设置，如果使用类中写好的默认设置则可以不传该参数
        """
        self.strategy = strategyClass(self, setting)
        if not self.strategy.name:
            self.strategy.name = self.strategy.className

        self.strategy.onInit()
        self.strategy.onStart()

    # ---------------------------------------------------------------------
    def saveStrategyData(self):
        """保存策略数据"""
        if self.strategy is None:
            return
        self.writeCtaLog(u'save strategy data')
        self.strategy.saveData()

    #----------------------------------------------------------------------
    def sendOrder(self, vtSymbol, orderType, price, volume, strategy, priceType=PRICETYPE_LIMITPRICE):
        """发单"""

        self.limitOrderCount += 1
        orderID = str(self.limitOrderCount)
        
        order = VtOrderData()
        order.vtSymbol = vtSymbol
        order.price = self.roundToPriceTick(price)
        order.totalVolume = volume
        order.status = STATUS_NOTTRADED     # 刚提交尚未成交
        order.orderID = orderID
        order.vtOrderID = orderID
        order.orderTime = str(self.dt)

        # added by IncenseLee
        order.gatewayName = self.gatewayName
        
        # CTA委托类型映射
        if orderType == CTAORDER_BUY:
            order.direction = DIRECTION_LONG
            order.offset = OFFSET_OPEN
        elif orderType == CTAORDER_SELL:
            order.direction = DIRECTION_SHORT
            order.offset = OFFSET_CLOSE
        elif orderType == CTAORDER_SHORT:
            order.direction = DIRECTION_SHORT
            order.offset = OFFSET_OPEN
        elif orderType == CTAORDER_COVER:
            order.direction = DIRECTION_LONG
            order.offset = OFFSET_CLOSE     

        # modified by IncenseLee
        key = u'{0}.{1}'.format(order.gatewayName, orderID)
        # 保存到限价单字典中
        self.workingLimitOrderDict[key] = order
        self.limitOrderDict[key] = order

        self.writeCtaLog(u'{},{},p:{},v:{},ref:{}'.format(vtSymbol, orderType, price, volume,key))
        return key
    
    #----------------------------------------------------------------------
    def cancelOrder(self, vtOrderID):
        """撤单"""
        if vtOrderID in self.workingLimitOrderDict:
            order = self.workingLimitOrderDict[vtOrderID]
            order.status = STATUS_CANCELLED
            order.cancelTime = str(self.dt)
            del self.workingLimitOrderDict[vtOrderID]

    def cancelOrders(self, symbol, offset=EMPTY_STRING):
        """撤销所有单"""
        # Symbol参数:指定合约的撤单；
        # OFFSET参数:指定Offset的撤单,缺省不填写时，为所有
        self.writeCtaLog(u'从所有订单中撤销{0}\{1}'.format(offset, symbol))
        for vtOrderID in self.workingLimitOrderDict.keys():
            order = self.workingLimitOrderDict[vtOrderID]

            if offset == EMPTY_STRING:
                offsetCond = True
            else:
                offsetCond = order.offset == offset

            if order.symbol == symbol and offsetCond:
                self.writeCtaLog(u'撤销订单:{0},{1} {2}@{3}'.format(vtOrderID, order.direction, order.price, order.totalVolume))
                order.status = STATUS_CANCELLED
                order.cancelTime = str(self.dt)
                del self.workingLimitOrderDict[vtOrderID]

    #----------------------------------------------------------------------
    def sendStopOrder(self, vtSymbol, orderType, price, volume, strategy):
        """发停止单（本地实现）"""

        self.stopOrderCount += 1
        stopOrderID = STOPORDERPREFIX + str(self.stopOrderCount)
        
        so = StopOrder()
        so.vtSymbol = vtSymbol
        so.price = self.roundToPriceTick(price)
        so.volume = volume
        so.strategy = strategy
        so.stopOrderID = stopOrderID
        so.status = STOPORDER_WAITING
        
        # added by IncenseLee
        so.gatewayName = STOPORDERPREFIX[0:-1]
        so.orderId = str(self.stopOrderCount)

        if orderType == CTAORDER_BUY:
            so.direction = DIRECTION_LONG
            so.offset = OFFSET_OPEN
        elif orderType == CTAORDER_SELL:
            so.direction = DIRECTION_SHORT
            so.offset = OFFSET_CLOSE
        elif orderType == CTAORDER_SHORT:
            so.direction = DIRECTION_SHORT
            so.offset = OFFSET_OPEN
        elif orderType == CTAORDER_COVER:
            so.direction = DIRECTION_LONG
            so.offset = OFFSET_CLOSE           
        
        # 保存stopOrder对象到字典中
        self.stopOrderDict[stopOrderID] = so
        self.workingStopOrderDict[stopOrderID] = so
        
        return stopOrderID
    
    #----------------------------------------------------------------------
    def cancelStopOrder(self, stopOrderID):
        """撤销停止单"""
        # 检查停止单是否存在
        if stopOrderID in self.workingStopOrderDict:
            so = self.workingStopOrderDict[stopOrderID]
            so.status = STOPORDER_CANCELLED
            del self.workingStopOrderDict[stopOrderID]
            
    #----------------------------------------------------------------------
    def crossLimitOrder(self):
        """基于最新数据撮合限价单"""
        # 先确定会撮合成交的价格
        if self.mode == self.BAR_MODE:
            buyCrossPrice = self.roundToPriceTick(self.bar.low) + self.priceTick        # 若买入方向限价单价格高于该价格，则会成交
            sellCrossPrice = self.roundToPriceTick(self.bar.high) - self.priceTick      # 若卖出方向限价单价格低于该价格，则会成交
            buyBestCrossPrice = self.roundToPriceTick(self.bar.open) + self.priceTick   # 在当前时间点前发出的买入委托可能的最优成交价
            sellBestCrossPrice = self.roundToPriceTick(self.bar.open) - self.priceTick  # 在当前时间点前发出的卖出委托可能的最优成交价
            vtSymbol = self.bar.vtSymbol
            symbol = self.bar.symbol
        else:
            buyCrossPrice = self.tick.askPrice1
            sellCrossPrice = self.tick.bidPrice1
            buyBestCrossPrice = self.tick.askPrice1
            sellBestCrossPrice = self.tick.bidPrice1
            vtSymbol = self.tick.vtSymbol
            symbol = self.tick.symbol

        # 遍历限价单字典中的所有限价单
        workingLimitOrderDictClone = copy.deepcopy(self.workingLimitOrderDict)
        for orderID, order in list(workingLimitOrderDictClone.items()):
            # 判断是否会成交
            buyCross = order.direction == DIRECTION_LONG and order.price >= buyCrossPrice and (vtSymbol.lower() == order.vtSymbol.lower() or symbol.lower() == order.vtSymbol.lower())
            sellCross = order.direction == DIRECTION_SHORT and order.price <= sellCrossPrice and (vtSymbol.lower() == order.vtSymbol.lower() or symbol.lower() == order.vtSymbol.lower())
            
            # 如果发生了成交
            if buyCross or sellCross:
                # 推送成交数据
                self.tradeCount += 1            # 成交编号自增1

                tradeID = str(self.tradeCount)
                trade = VtTradeData()
                trade.vtSymbol = order.vtSymbol
                trade.tradeID = tradeID
                trade.vtTradeID = tradeID
                trade.orderID = order.orderID
                trade.vtOrderID = order.orderID
                trade.direction = order.direction
                trade.offset = order.offset
                
                # 以买入为例：
                # 1. 假设当根K线的OHLC分别为：100, 125, 90, 110
                # 2. 假设在上一根K线结束(也是当前K线开始)的时刻，策略发出的委托为限价105
                # 3. 则在实际中的成交价会是100而不是105，因为委托发出时市场的最优价格是100
                if buyCross:
                    if self.useBreakoutMode is False:
                        trade.price = min(order.price, buyBestCrossPrice)
                    else:
                        trade.price = max(order.price, buyBestCrossPrice)
                    self.strategy.pos += order.totalVolume
                else:
                    if self.useBreakoutMode is False:
                        trade.price = max(order.price, sellBestCrossPrice)
                    else:
                        trade.price = min(order.price, sellBestCrossPrice)
                    self.strategy.pos -= order.totalVolume
                
                trade.volume = order.totalVolume
                trade.tradeTime = str(self.dt)
                trade.dt = self.dt
                self.strategy.onTrade(trade)
                
                self.tradeDict[tradeID] = trade
                self.writeCtaLog(u'TradeId:{0}'.format(tradeID))

                # 更新持仓缓存数据      # TODO: do we need this?
                posBuffer = self.posBufferDict.get(trade.vtSymbol, None)
                if not posBuffer:
                    posBuffer = PositionBuffer()
                    posBuffer.vtSymbol = trade.vtSymbol
                    self.posBufferDict[trade.vtSymbol] = posBuffer
                posBuffer.updateTradeData(trade)
                self.writeCtaLog(u'DEBUG-- [ctaBacktesting] crossLimitOrder: TradeId:{},  posBuffer = {}'.format(tradeID, posBuffer.toStr()))

                # 推送委托数据
                order.tradedVolume = order.totalVolume
                order.status = STATUS_ALLTRADED

                self.strategy.onOrder(order)
                
                # 从字典中删除该限价单
                try:
                    del self.workingLimitOrderDict[orderID]
                except Exception as ex:
                    self.writeCtaError(u'crossLimitOrder exception:{},{}'.format(str(ex), traceback.format_exc()))

        # 实时计算模式
        if self.calculateMode == self.REALTIME_MODE:
            self.realtimeCalculate()
                
    #----------------------------------------------------------------------
    def crossStopOrder(self):
        """基于最新数据撮合停止单"""
        # 先确定会撮合成交的价格，这里和限价单规则相反
        if self.mode == self.BAR_MODE:
            buyCrossPrice = self.bar.high    # 若买入方向停止单价格低于该价格，则会成交
            sellCrossPrice = self.bar.low    # 若卖出方向限价单价格高于该价格，则会成交
            bestCrossPrice = self.bar.open   # 最优成交价，买入停止单不能低于，卖出停止单不能高于
            vtSymbol = self.bar.vtSymbol
            symbol = self.bar.symbol
        else:
            buyCrossPrice = self.tick.lastPrice
            sellCrossPrice = self.tick.lastPrice
            bestCrossPrice = self.tick.lastPrice
            vtSymbol = self.tick.vtSymbol
            symbol = self.tick.symbol

        # 遍历停止单字典中的所有停止单
        workingStopOrderDictClone = copy.deepcopy(self.workingStopOrderDict)
        for stopOrderID, so in workingStopOrderDictClone.items():
            # 判断是否会成交
            buyCross = so.direction == DIRECTION_LONG and so.price <= buyCrossPrice and (vtSymbol.lower() == so.vtSymbol.lower() or symbol.lower() == order.vtSymbol.lower())
            sellCross = so.direction == DIRECTION_SHORT and so.price >= sellCrossPrice and (vtSymbol.lower() == so.vtSymbol.lower() or symbol.lower() == order.vtSymbol.lower())
            
            # 如果发生了成交
            if buyCross or sellCross:
                # 推送成交数据
                self.tradeCount += 1            # 成交编号自增1
                tradeID = str(self.tradeCount)
                trade = VtTradeData()
                trade.vtSymbol = so.vtSymbol
                trade.tradeID = tradeID
                trade.vtTradeID = tradeID
                
                if buyCross:
                    self.strategy.pos += so.volume
                    trade.price = max(bestCrossPrice, so.price)
                else:
                    self.strategy.pos -= so.volume
                    trade.price = min(bestCrossPrice, so.price)                
                
                self.limitOrderCount += 1
                orderID = str(self.limitOrderCount)
                trade.orderID = orderID
                trade.vtOrderID = orderID
                
                trade.direction = so.direction
                trade.offset = so.offset
                trade.volume = so.volume
                trade.tradeTime = str(self.dt)
                trade.dt = self.dt
                self.strategy.onTrade(trade)
                
                self.tradeDict[tradeID] = trade
                
                # 更新持仓缓存数据      # TODO: do we need this?
                posBuffer = self.posBufferDict.get(trade.vtSymbol, None)
                if not posBuffer:
                    posBuffer = PositionBuffer()
                    posBuffer.vtSymbol = trade.vtSymbol
                    self.posBufferDict[trade.vtSymbol] = posBuffer
                posBuffer.updateTradeData(trade)

                # 推送委托数据
                so.status = STOPORDER_TRIGGERED
                
                order = VtOrderData()
                order.vtSymbol = so.vtSymbol
                order.symbol = so.vtSymbol
                order.orderID = orderID
                order.vtOrderID = orderID
                order.direction = so.direction
                order.offset = so.offset
                order.price = so.price
                order.totalVolume = so.volume
                order.tradedVolume = so.volume
                order.status = STATUS_ALLTRADED
                order.orderTime = trade.tradeTime
                order.gatewayName = so.gatewayName
                self.strategy.onOrder(order)
                
                self.limitOrderDict[orderID] = order
                
                # 从字典中删除该限价单
                try:
                    del self.workingStopOrderDict[stopOrderID]
                except Exception as ex:
                    self.writeCtaError(u'crossStopOrder exception:{},{}'.format(str(ex), traceback.format_exc()))

        # 若采用实时计算净值
        if self.calculateMode == self.REALTIME_MODE:
            self.realtimeCalculate()


    #----------------------------------------------------------------------
    def insertData(self, dbName, collectionName, data):
        """考虑到回测中不允许向数据库插入数据，防止实盘交易中的一些代码出错"""
        pass
    
    #----------------------------------------------------------------------
    def loadBar(self, dbName, collectionName, startDate):
        """直接返回初始化数据列表中的Bar"""
        return self.initData
    
    #----------------------------------------------------------------------
    def loadTick(self, dbName, collectionName, startDate):
        """直接返回初始化数据列表中的Tick"""
        return self.initData

    def get_data_path(self):
        """
        获取数据保存目录
        :return:
        """
        logs_folder = os.path.abspath(os.path.join(os.getcwd(), 'data'))
        if os.path.exists(logs_folder):
            return logs_folder
        else:
            return os.path.abspath(os.path.join(cta_engine_path, 'data'))

    def get_logs_path(self):
        """
        获取日志保存目录
        :return:
        """
        logs_folder = os.path.abspath(os.path.join(os.getcwd(), 'logs'))
        if os.path.exists(logs_folder):
            return logs_folder
        else:
            return os.path.abspath(os.path.join(cta_engine_path, 'TestLogs'))

    def createLogger(self, debug=False):
        """
        创建日志
        :param debug:
        :return:
        """
        filename = os.path.abspath(os.path.join(self.get_logs_path(), '{}'.format(self.strategy_name if len(self.strategy_name) > 0 else 'strategy')))
        self.logger = setup_logger(filename=filename, name=self.strategy_name if len(self.strategy_name) > 0 else 'strategy', debug=debug,backtesing=True)

    #----------------------------------------------------------------------
    def writeCtaLog(self, content,strategy_name=None):
        """记录日志"""
        #log = str(self.dt) + ' ' + content
        #self.logList.append(log)

        # 写入本地log日志
        if self.logger:
            self.logger.info(content)
        else:
            self.createLogger()

    def writeCtaError(self, content,strategy_name=None):
        """记录异常"""
        self.output(u'Error:{}'.format(content))
        if self.logger:
            self.logger.error(content)
        else:
            self.createLogger()

    def writeCtaWarning(self, content,strategy_name=None):
        """记录告警"""
        self.output(u'Warning:{}'.format(content))
        if self.logger:
            self.logger.warning(content)
        else:
            self.createLogger()

    def writeCtaNotification(self,content,strategy_name=None):
        """记录通知"""
        #print content
        self.output(u'Notify:{}'.format(content))
        self.writeCtaLog(content)

    #----------------------------------------------------------------------
    def output(self, content):
        """输出内容"""
        #print str(datetime.now()) + "\t" + content
        pass

    def realtimeCalculate(self):
        """实时计算交易结果2
        支持多空仓位并存"""

        if len(self.tradeDict) < 1: return

        tradeids = list(self.tradeDict.keys())
        #resultDict = OrderedDict()  # 交易结果记录
        resultDict = []
        longid = EMPTY_STRING
        shortid = EMPTY_STRING

        # 对交易记录逐一处理
        for tradeid in tradeids:
            try:
                trade = self.tradeDict[tradeid]
            except:
                self.writeCtaError(u'没有{0}的成交单'.format(tradeid))
                continue

            # buy trade
            if trade.direction == DIRECTION_LONG and trade.offset == OFFSET_OPEN:
                self.output(u'{0}多开:{1},{2}'.format(trade.vtSymbol, trade.volume, trade.price))
                self.writeCtaLog(u'{0}多开:{1},{2}'.format(trade.vtSymbol, trade.volume, trade.price))
                self.longPosition.append(trade)
                del self.tradeDict[tradeid]

            if trade.volume == EMPTY_INT:
                self.writeCtaLog(u'{},dir:{},vtOrderID:{}tradeID:{}的volumn为{},删除'.format(trade.vtSymbol, trade.direction,trade.vtOrderID,trade.tradeID,trade.volume))
                try:
                    del self.tradeDict[tradeid]
                except:
                    pass
                continue

            # cover trade，
            elif trade.direction == DIRECTION_LONG and trade.offset == OFFSET_CLOSE:
                gId = trade.tradeID    # 交易组（多个平仓数为一组）
                gr = None       # 组合的交易结果

                coverVolume = trade.volume
                self.writeCtaLog(u'平空:{}'.format(coverVolume))
                while coverVolume > 0:
                    if len(self.shortPosition) == 0:
                        self.writeCtaError(u'异常!没有开空仓的数据')
                        raise Exception(u'realtimeCalculate2() Exception,没有开空仓的数据')
                        return
                    cur_short_pos_list = [s_pos.volume for s_pos in self.shortPosition]
                    self.writeCtaLog(u'当前空单:{}'.format(cur_short_pos_list))
                    pop_indexs = [i for i, val in enumerate(self.shortPosition) if val.vtSymbol == trade.vtSymbol]
                    if len(pop_indexs) < 1:
                        self.writeCtaError(u'异常，没有对应symbol:{0}的空单持仓'.format(trade.vtSymbol))
                        raise Exception(u'realtimeCalculate2() Exception,没有对应symbol:{0}的空单持仓'.format(trade.vtSymbol))
                        return

                    pop_index = pop_indexs[0]
                    # 从未平仓的空头交易
                    entryTrade = self.shortPosition.pop(pop_index)

                    # 开空volume，不大于平仓volume
                    if coverVolume >= entryTrade.volume:
                        self.writeCtaLog(u'开空volume，不大于平仓volume, coverVolume:{} ,先平::{}'.format(coverVolume, entryTrade.volume))
                        coverVolume = coverVolume - entryTrade.volume
                        if coverVolume>0:
                            self.writeCtaLog(u'剩余待平数量:{}'.format(coverVolume))
                        self.output(u'{0}空平:{1},{2}'.format(entryTrade.vtSymbol, entryTrade.volume, trade.price))
                        self.writeCtaLog(u'{0}空平:{1},{2}'.format(entryTrade.vtSymbol, entryTrade.volume, trade.price))

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=-entryTrade.volume,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Short'
                        t['CloseTime'] = trade.tradeTime
                        t['ClosePrice'] = trade.price
                        t['Volume'] = entryTrade.volume
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        msg = u'Gid:{0} {1}[{2}:开空tid={3}:{4}]-[{5}.平空tid={6},{7},vol:{8}],净盈亏pnl={9},手续费:{10}'\
                            .format(gId, entryTrade.vtSymbol, entryTrade.tradeTime, shortid, entryTrade.price,
                                                 trade.tradeTime, tradeid, trade.price,
                                                 entryTrade.volume, result.pnl,result.commission)
                        self.output(msg)
                        self.writeCtaLog(msg)
                        resultDict.append(result)

                        if type(gr) == type(None):
                            if coverVolume > 0:
                                # 属于组合
                                gr = copy.deepcopy(result)
                            else:
                                # 删除平空交易单，
                                self.writeCtaLog(u'删除平空交易单，tradeID:'.format(trade.tradeID))
                                del self.tradeDict[trade.tradeID]

                        else:
                            # 更新组合的数据
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl

                            # 所有仓位平完
                            if coverVolume == 0:
                                self.writeCtaLog(u'所有平空仓位撮合完毕')
                                gr.volume = abs(trade.volume)
                                #resultDict[entryTrade.dt] = gr
                                # 删除平空交易单，
                                self.writeCtaLog(u'删除平空交易单:{}'.format(trade.tradeID))
                                del self.tradeDict[trade.tradeID]

                    # 开空volume,大于平仓volume，需要更新减少tradeDict的数量。
                    else:
                        self.writeCtaLog(u'Short volume:{0} > Cover volume:{1}，需要更新减少tradeDict的数量。'.format(entryTrade.volume,coverVolume))
                        shortVolume = entryTrade.volume - coverVolume

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=-coverVolume,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Short'
                        t['CloseTime'] = trade.tradeTime
                        t['ClosePrice'] = trade.price
                        t['Volume'] = coverVolume
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        msg = u'Gid:{0} {1}[{2}:开空tid={3}:{4}]-[{5}.平空tid={6},{7},vol:{8}],净盈亏pnl={9},手续费:{10}'\
                            .format(gId, entryTrade.vtSymbol, entryTrade.tradeTime, shortid, entryTrade.price,
                                                 trade.tradeTime, tradeid, trade.price,
                                                 coverVolume, result.pnl,result.commission)
                        self.output(msg)
                        self.writeCtaLog(msg)

                        # 更新（减少）开仓单的volume,重新推进开仓单列表中
                        entryTrade.volume = shortVolume
                        self.writeCtaLog(u'更新（减少）开仓单的volume,重新推进开仓单列表中:{}'.format(entryTrade.volume))
                        self.shortPosition.append(entryTrade)
                        cur_short_pos_list = [s_pos.volume for s_pos in self.shortPosition]
                        self.writeCtaLog(u'当前空单:{}'.format(cur_short_pos_list))

                        coverVolume = 0
                        resultDict.append(result)

                        if type(gr) != type(None):
                            # 更新组合的数据
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl
                            gr.volume = abs(trade.volume)

                        # 删除平空交易单，
                        del self.tradeDict[trade.tradeID]

                if type(gr) != type(None):
                    self.writeCtaLog(u'组合净盈亏:{0}'.format(gr.pnl))

                self.writeCtaLog(u'-------------')

            # Short Trade
            elif trade.direction == DIRECTION_SHORT and trade.offset == OFFSET_OPEN:
                self.output(u'{0}空开:{1},{2}'.format(trade.vtSymbol, trade.volume, trade.price))
                self.writeCtaLog(u'{0}空开:{1},{2}'.format(trade.vtSymbol, trade.volume, trade.price))
                self.shortPosition.append(trade)
                del self.tradeDict[trade.tradeID]
                continue

            # sell trade
            elif trade.direction == DIRECTION_SHORT and trade.offset == OFFSET_CLOSE:
                gId = trade.tradeID  # 交易组（多个平仓数为一组）
                gr = None           # 组合的交易结果

                sellVolume = trade.volume

                while sellVolume > 0:
                    if len(self.longPosition) == 0:
                        self.writeCtaError(u'异常，没有开多单')
                        raise RuntimeError(u'realtimeCalculate2() Exception,没有开多单')
                        return

                    pop_indexs = [i for i, val in enumerate(self.longPosition) if val.vtSymbol == trade.vtSymbol]
                    if len(pop_indexs) < 1:
                        self.writeCtaError(u'没有对应的symbol{0}多单数据,'.format(trade.vtSymbol))
                        raise RuntimeError(u'realtimeCalculate2() Exception,没有对应的symbol{0}多单数据,'.format(trade.vtSymbol))
                        return

                    pop_index = pop_indexs[0]
                    entryTrade = self.longPosition.pop(pop_index)
                     # 开多volume，不大于平仓volume
                    if sellVolume >= entryTrade.volume:
                        self.writeCtaLog(u'{0}Sell Volume:{1} >= Entry Volume:{2}'.format(entryTrade.vtSymbol, sellVolume, entryTrade.volume))
                        sellVolume = sellVolume - entryTrade.volume
                        self.output(u'{0}多平:{1},{2}'.format(entryTrade.vtSymbol, entryTrade.volume, trade.price))
                        self.writeCtaLog(u'{0}多平:{1},{2}'.format(entryTrade.vtSymbol, entryTrade.volume, trade.price))

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=entryTrade.volume,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Long'
                        t['CloseTime'] = trade.tradeTime
                        t['ClosePrice'] = trade.price
                        t['Volume'] = entryTrade.volume
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        msg = u'Gid:{0} {1}[{2}:开多tid={3}:{4}]-[{5}.平多tid={6},{7},vol:{8}],净盈亏pnl={9},手续费:{10}'\
                            .format(gId, entryTrade.vtSymbol,
                                        entryTrade.tradeTime, longid, entryTrade.price,
                                        trade.tradeTime, tradeid, trade.price,
                                        entryTrade.volume, result.pnl, result.commission)
                        self.output(msg)
                        self.writeCtaLog(msg)
                        resultDict.append(result)

                        if type(gr) == type(None):
                            if sellVolume > 0:
                                # 属于组合
                                gr = copy.deepcopy(result)

                            else:
                                # 删除平多交易单，
                                del self.tradeDict[trade.tradeID]

                        else:
                            # 更新组合的数据
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl

                            if sellVolume == 0:
                                gr.volume = abs(trade.volume)
                                # 删除平多交易单，
                                del self.tradeDict[trade.tradeID]

                    # 开多volume,大于平仓volume，需要更新减少tradeDict的数量。
                    else:
                        longVolume = entryTrade.volume -sellVolume
                        self.writeCtaLog(u'Entry Long Volume:{0} > Sell Volume:{1},Remain:{2}'
                                         .format(entryTrade.volume, sellVolume, longVolume))

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=sellVolume,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Long'
                        t['CloseTime'] = trade.tradeTime
                        t['ClosePrice'] = trade.price
                        t['Volume'] = sellVolume
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        msg = u'Gid:{0} {1}[{2}:开多tid={3}:{4}]-[{5}.平多tid={6},{7},vol:{8}],净盈亏pnl={9},手续费:{10}'\
                            .format(gId, entryTrade.vtSymbol,entryTrade.tradeTime, longid, entryTrade.price,
                                    trade.tradeTime, tradeid, trade.price, sellVolume, result.pnl, result.commission)
                        self.output(msg)
                        self.writeCtaLog(msg)

                        # 减少开多volume,重新推进多单持仓列表中
                        entryTrade.volume = longVolume
                        self.longPosition.append(entryTrade)

                        sellVolume = 0
                        resultDict.append(result)

                        if type(gr) != type(None):
                            # 更新组合的数据
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl
                            gr.volume = abs(trade.volume)

                        # 删除平多交易单，
                        del self.tradeDict[trade.tradeID]

                if type(gr) != type(None):
                    self.writeCtaLog(u'组合净盈亏:{0}'.format(gr.pnl))

                self.writeCtaLog(u'-------------')

        # 计算仓位比例
        occupyMoney = EMPTY_FLOAT
        occupyLongVolume = EMPTY_INT
        occupyShortVolume = EMPTY_INT
        longPos = {}
        shortPos = {}
        if len(self.longPosition) > 0:
            for t in self.longPosition:
                occupyMoney += t.price * abs(t.volume) * self.size * self.margin_rate
                occupyLongVolume += abs(t.volume)
                if t.vtSymbol in longPos:
                    longPos[t.vtSymbol] += abs(t.volume)
                else:
                    longPos[t.vtSymbol] = abs(t.volume)

        if len(self.shortPosition) > 0:
            for t in self.shortPosition:
                occupyMoney += t.price * abs(t.volume) * self.size * self.margin_rate
                occupyShortVolume += (t.volume)
                if t.vtSymbol in shortPos:
                    shortPos[t.vtSymbol] += abs(t.volume)
                else:
                    shortPos[t.vtSymbol] = abs(t.volume)

        self.output(u'L:{0}|{1},S:{2}|{3}'.format(occupyLongVolume, str(longPos), occupyShortVolume, str(shortPos)))
        self.writeCtaLog(u'L:{0}|{1},S:{2}|{3}'.format(occupyLongVolume, str(longPos), occupyShortVolume, str(shortPos)))
        # 最大持仓
        self.maxVolume = max(self.maxVolume, occupyLongVolume + occupyShortVolume)

        # 更改为持仓净值
        self.avaliable = self.netCapital - occupyMoney
        self.percent = round(float(occupyMoney * 100 / self.netCapital), 2)

        # 检查是否有平交易
        if len(resultDict) ==0:
            msg = u''
            if len(self.longPosition) > 0:
                msg += u'持多仓{0},'.format( str(longPos))

            if len(self.shortPosition) > 0:
                msg += u'持空仓{0},'.format(str(shortPos))

            msg += u'资金占用:{0},仓位:{1}%%'.format(occupyMoney, self.percent)
            self.output(msg)
            self.writeCtaLog(msg)
            return

        # 对交易结果汇总统计
        for result in resultDict:

            if result.pnl > 0:
                self.winningResult += 1
                self.totalWinning += result.pnl
            else:
                self.losingResult += 1
                self.totalLosing += result.pnl
            self.capital += result.pnl
            self.maxCapital = max(self.capital, self.maxCapital)
            self.netCapital = max(self.netCapital, self.capital)
            self.maxNetCapital = max(self.netCapital,self.maxNetCapital)
            #self.maxVolume = max(self.maxVolume, result.volume)
            drawdown = self.capital - self.maxCapital
            drawdownRate = round(float(drawdown*100/self.maxCapital),4)

            self.pnlList.append(result.pnl)
            self.timeList.append(result.exitDt)
            self.capitalList.append(self.capital)
            self.drawdownList.append(drawdown)
            self.drawdownRateList.append(drawdownRate)

            self.totalResult += 1
            self.totalTurnover += result.turnover
            self.totalCommission += result.commission
            self.totalSlippage += result.slippage

            msg =u'[gid:{}] {} 交易盈亏:{},交易手续费:{}回撤:{}/{},账号平仓权益:{},持仓权益：{}，累计手续费:{}'\
                .format(result.groupId, result.exitDt, result.pnl, result.commission, drawdown,
                        drawdownRate, self.capital,self.netCapital, self.totalCommission )
            self.output(msg)
            self.writeCtaLog(msg)

        # 重新计算一次avaliable
        self.avaliable = self.netCapital - occupyMoney
        self.percent = round(float(occupyMoney * 100 / self.netCapital), 2)

    def savingDailyData(self, d, c, m, commission, benchmark=0):
        """保存每日数据"""
        dict = {}
        dict['date'] = d.strftime('%Y/%m/%d')
        dict['capital'] = c
        dict['maxCapital'] = m
        long_list = []
        today_margin = 0
        today_margin_long = 0
        today_margin_short = 0
        long_pos_occupy_money = 0
        short_pos_occupy_money = 0

        if self.daily_first_benchmark is None and benchmark >0:
            self.daily_first_benchmark = benchmark

        if benchmark > 0 and self.daily_first_benchmark is not None and self.daily_first_benchmark > 0:
            benchmark = benchmark / self.daily_first_benchmark
        else:
            benchmark = 1

        positionMsg = ""
        for longpos in self.longPosition:
            symbol = '-' if longpos.vtSymbol == EMPTY_STRING else longpos.vtSymbol
            # 计算持仓浮盈浮亏/占用保证金
            pos_margin = 0
            if self.last_leg1_tick is not None and (self.last_leg1_tick.vtSymbol == symbol or self.last_leg1_tick.symbol == symbol):
                pos_margin = (self.last_leg1_tick.lastPrice - longpos.price) * longpos.volume * self.size
                long_pos_occupy_money += self.last_leg1_tick.lastPrice * abs(longpos.volume) * self.size * self.margin_rate

            elif self.last_leg2_tick is not None and (self.last_leg2_tick.vtSymbol == symbol or self.last_leg2_tick.symbol == symbol):
                pos_margin = (self.last_leg2_tick.lastPrice - longpos.price) * longpos.volume * self.size
                long_pos_occupy_money += self.last_leg2_tick.lastPrice * abs(longpos.volume) * self.size * self.margin_rate

            elif self.last_bar is not None:
                pos_margin = (self.last_bar.close - longpos.price) * longpos.volume * self.size
                long_pos_occupy_money += self.last_bar.close * abs(
                    longpos.volume) * self.size * self.margin_rate

            today_margin += pos_margin
            today_margin_long += pos_margin
            long_list.append({'symbol': symbol, 'direction':'long','price':longpos.price,'volume':longpos.volume,'margin':pos_margin})
            positionMsg += "{},long,p={},v={},m={};".format(symbol,longpos.price,longpos.volume,pos_margin)

        short_list = []
        for shortpos in self.shortPosition:
            symbol = '-' if shortpos.vtSymbol == EMPTY_STRING else shortpos.vtSymbol
            # 计算持仓浮盈浮亏/占用保证金
            pos_margin = 0
            if self.last_leg1_tick is not None and (self.last_leg1_tick.vtSymbol == symbol or self.last_leg1_tick.symbol == symbol):
                pos_margin = (shortpos.price - self.last_leg1_tick.lastPrice) * shortpos.volume * self.size
                short_pos_occupy_money += self.last_leg1_tick.lastPrice * abs(shortpos.volume) * self.size * self.margin_rate

            elif self.last_leg2_tick is not None and (self.last_leg2_tick.vtSymbol == symbol or self.last_leg2_tick.symbol == symbol):
                pos_margin = (shortpos.price - self.last_leg2_tick.lastPrice) * shortpos.volume * self.size
                short_pos_occupy_money += self.last_leg2_tick.lastPrice * abs( shortpos.volume) * self.size * self.margin_rate
            elif self.last_bar is not None:
                pos_margin = (shortpos.price - self.last_bar.close) * shortpos.volume * self.size
                short_pos_occupy_money += self.last_bar.close * abs(
                    shortpos.volume) * self.size * self.margin_rate
            today_margin += pos_margin
            today_margin_short += pos_margin
            short_list.append({'symbol': symbol, 'direction': 'short', 'price': shortpos.price,
                               'volume': shortpos.volume, 'margin': pos_margin})
            positionMsg += "{},short,p={},v={},m={};".format(symbol,shortpos.price,shortpos.volume,pos_margin)

        dict['net'] = c + today_margin
        dict['rate'] = (c + today_margin )/ self.initCapital
        dict['longPos'] = json.dumps(long_list, indent=4)
        dict['shortPos'] = json.dumps(short_list, indent=4)
        dict['longMoney'] = long_pos_occupy_money
        dict['shortMoney'] = short_pos_occupy_money
        dict['occupyMoney'] = max(long_pos_occupy_money, short_pos_occupy_money)
        dict['occupyRate'] = dict['occupyMoney'] / dict['capital']
        dict['commission'] = commission
        dict['benchmark'] = benchmark
        dict['todayMarginLong'] = today_margin_long
        dict['todayMarginShort'] = today_margin_short
        self.last_leg1_tick = None
        if self.tick is not None:
            dict['lastPrice'] = self.tick.lastPrice
        elif self.last_leg1_tick is not None:
            dict['lastPrice'] = self.last_leg1_tick.lastPrice
        elif self.last_leg2_tick is not None:
            dict['lastPrice'] = self.last_leg2_tick.lastPrice
        elif self.last_bar is not None:
            dict['lastPrice'] = self.last_bar.close
        else:
            dict['lastPrice'] = self.dailyList[-1]['lastPrice']

        self.dailyList.append(dict)

        # 更新每日浮动净值
        self.netCapital = dict['net']

        # 更新最大初次持仓浮盈净值
        if dict['net'] > self.maxNetCapital:
            self.maxNetCapital = dict['net']
            self.maxNetCapital_time = dict['date']
        drawdown_rate = round((float(self.maxNetCapital - dict['net'])*100)/m, 4)
        if drawdown_rate > self.daily_max_drawdown_rate:
            self.daily_max_drawdown_rate = drawdown_rate
            self.max_drowdown_rate_time = dict['date']

        self.writeCtaLog(u'DEBUG---: savingDailyData, {}: lastPrice={}, net={}, capital={} max={} margin={} commission={} longPos={} shortPos={}, {}'.format(
            dict['date'], dict['lastPrice'], dict['net'], c, m, today_margin, commission, len(long_list), len(short_list), positionMsg))

    # ----------------------------------------------------------------------
    def writeWenHuaSignal(self, filehandle, count, bardatetime, price, text, mask=52):
        """
        输出到文华信号
        :param filehandle:
        :param count:
        :param bardatetime:
        :param price:
        :param text:
        :param mask:  bit8~1 = [(H2)(H1)(M30)(M15)(M10)(M5)(M3)(M1)], e.g. 52 means M30, M15, M5
        :return:
        """
        # 文华信号
        bardatetime2 = bardatetime
        if bardatetime.hour >= 21:
            if bardatetime.isoweekday() == 5:
                # 星期五=》星期一
                bardatetime2 = bardatetime + timedelta(days=3)
            else:
                # 第二天
                bardatetime2 = bardatetime + timedelta(days=1)
        elif bardatetime.hour < 8 and bardatetime.isoweekday() == 6:
            # 星期六=>星期一
            bardatetime2 = bardatetime + timedelta(days=2)
        barDate = bardatetime2.strftime('%Y%m%d')
        barTime = bardatetime.strftime('%H%M')

        isFirst = False
        prefixMsg = '(AA{}'.format(count)
        outputMsg = 'AA{}:=DATE={};\n'.format(count, barDate[2:])
        filehandle.write(outputMsg)
        barTime = bardatetime.strftime('%H%M')
        if mask & 1 > 0:  # Min1
            outputMsg = 'BB{}:=PERIOD=1&&TIME={};\n'.format(count, barDate[2:], barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            prefixMsg += 'BB{}'.format(count)
        if mask & 2 > 0:   # Min3
            barTimeBegin = (bardatetime - timedelta(minutes=3)).strftime('%H%M')
            outputMsg = 'CC{}:=PERIOD=2&&TIME>{}&&TIME<={};\n'.format(count, barTimeBegin, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'CC{}'.format(count)
        if mask & 4 > 0:   # Min5
            barTimeBegin = (bardatetime - timedelta(minutes=5)).strftime('%H%M')
            outputMsg = 'DD{}:=PERIOD=3&&TIME>{}&&TIME<={};\n'.format(count, barTimeBegin, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'DD{}'.format(count)
        if mask & 8 > 0:   # Min10
            barTimeBegin = (bardatetime - timedelta(minutes=10)).strftime('%H%M')
            outputMsg = 'EE{}:=PERIOD=4&&TIME>{}&&TIME<={};\n'.format(count, barTimeBegin, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'EE{}'.format(count)
        if mask & 16 > 0:   # Min15
            barTimeBegin = (bardatetime - timedelta(minutes=15)).strftime('%H%M')
            outputMsg = 'FF{}:=PERIOD=5&&TIME>{}&&TIME<={};\n'.format(count, barTimeBegin, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'FF{}'.format(count)
        if mask & 32 > 0:   # Min30
            barTimeBegin = (bardatetime - timedelta(minutes=30)).strftime('%H%M')
            if bardatetime.hour == 10:
                if bardatetime.minute >30 and bardatetime.minute < 45:
                    barTimeBegin = (bardatetime - timedelta(minutes=45)).strftime('%H%M')
            elif bardatetime.hour == 13:
                if bardatetime.minute < 45:
                    barTimeBegin = (bardatetime - timedelta(minutes=150)).strftime('%H%M')
            outputMsg = 'GG{}:=PERIOD=6&&TIME>{}&&TIME<={};\n'.format(count, barTimeBegin, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'GG{}'.format(count)
        if mask & 64 > 0:   # Hour1
            outputMsg = 'HH{}:=PERIOD=7&&TIME>{}-59&&TIME<={};\n'.format(count, barTime, barTime)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'HH{}'.format(count)
        if mask & 128 > 0:   # Hour2
            outputMsg = 'II{}:=PERIOD=8;\n'.format(count)
            filehandle.write(outputMsg)
            if isFirst is False:
                prefixMsg += ' AND ('
                isFirst = True
            else:
                prefixMsg += ' OR '
            prefixMsg += 'II{}'.format(count)
        if isFirst is True:
            prefixMsg += ')'

        outputMsg = 'DRAWICON' + prefixMsg + ', {}, \'ICO14\');\n'.format(price)
        filehandle.write(outputMsg)
        outputMsg = 'DRAWTEXT' + prefixMsg + ', H, \'{}\');\n'.format(text)
        filehandle.write(outputMsg)
        filehandle.flush()

    # ----------------------------------------------------------------------
    def calculateBacktestingResult(self):
        """
        计算回测结果
        Modified by Incense Lee
        增加了支持逐步加仓的计算：
        例如，前面共有6次开仓（1手开仓+5次加仓，每次1手），平仓只有1次（六手）。那么，交易次数是6次（开仓+平仓）。
        暂不支持每次加仓数目不一致的核对（因为比较复杂）

        增加组合的支持。（组合中，仍然按照1手逐步加仓和多手平仓的方法，即使启用了复利模式，也仍然按照这个规则，只是在计算收益时才乘以系数）

        增加期初权益，每次交易后的权益，可用资金，仓位比例。

        """
        self.output(u'计算回测结果')
        
        # 首先基于回测后的成交记录，计算每笔交易的盈亏
        resultDict = OrderedDict()  # 交易结果记录
        longTrade = []              # 未平仓的多头交易
        shortTrade = []             # 未平仓的空头交易

        i = 1

        tradeUnit = 1

        longid = EMPTY_STRING
        shortid = EMPTY_STRING

        for tradeid in self.tradeDict.keys():

            trade = self.tradeDict[tradeid]

            # 多头交易
            if trade.direction == DIRECTION_LONG:
                # 如果尚无空头交易
                if not shortTrade:
                    longTrade.append(trade)
                    longid = tradeid
                # 当前多头交易为平空
                else:
                    gId = i     # 交易组（多个平仓数为一组）
                    gt = 1      # 组合的交易次数
                    gr = None   # 组合的交易结果

                    if trade.volume >tradeUnit:
                        self.writeCtaLog(u'平仓数{0},组合编号:{1}'.format(trade.volume,gId))
                        gt = int(trade.volume/tradeUnit)

                    for tv in range(gt):

                        entryTrade = shortTrade.pop(0)

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=-tradeUnit,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)

                        if tv == 0:
                            if gt == 1:
                                resultDict[entryTrade.dt] = result
                            else:
                                gr = copy.deepcopy(result)
                        else:
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl

                            if tv == gt -1:
                                gr.volume = trade.volume
                                resultDict[entryTrade.dt] = gr

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime.strftime('%Y/%m/%d %H:%M:%S')
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Short'
                        t['CloseTime'] = trade.tradeTime.strftime('%Y/%m/%d %H:%M:%S')
                        t['ClosePrice'] = trade.price
                        t['Volume'] = tradeUnit
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        self.writeCtaLog(u'{9}@{6} [{7}:开空{0},short:{1}]-[{8}:平空{2},cover:{3},vol:{4}],净盈亏pnl={5}'
                                    .format(entryTrade.tradeTime, entryTrade.price,
                                            trade.tradeTime, trade.price, tradeUnit, result.pnl,
                                            i, shortid, tradeid, gId))
                        i = i+1

                    if type(gr) != type(None):
                        self.writeCtaLog(u'组合净盈亏:{0}'.format(gr.pnl))

                    self.writeCtaLog(u'-------------')

            # 空头交易        
            else:
                # 如果尚无多头交易
                if not longTrade:
                    shortTrade.append(trade)
                    shortid = tradeid
                # 当前空头交易为平多
                else:
                    gId = i     # 交易组（多个平仓数为一组）
                    gt = 1      # 组合的交易次数
                    gr = None   # 组合的交易结果

                    if trade.volume >tradeUnit:
                        self.writeCtaLog(u'平仓数{0},组合编号:{1}'.format(trade.volume,gId))
                        gt = int(trade.volume/tradeUnit)

                    for tv in range(gt):

                        entryTrade = longTrade.pop(0)

                        result = TradingResult(entryPrice=entryTrade.price,
                                               entryDt=entryTrade.dt,
                                               exitPrice=trade.price,
                                               exitDt=trade.dt,
                                               volume=tradeUnit,
                                               rate=self.rate,
                                               slippage=self.slippage,
                                               size=self.size,
                                               groupId=gId,
                                               fixcommission=self.fixCommission)
                        if tv == 0:
                            if gt==1:
                                resultDict[entryTrade.dt] = result
                            else:
                                gr = copy.deepcopy(result)
                        else:
                            gr.turnover = gr.turnover + result.turnover
                            gr.commission = gr.commission + result.commission
                            gr.slippage = gr.slippage + result.slippage
                            gr.pnl = gr.pnl + result.pnl

                            if tv == gt -1:
                                gr.volume = trade.volume
                                resultDict[entryTrade.dt] = gr

                        t = OrderedDict()
                        t['Gid'] = gId
                        t['vtSymbol'] = entryTrade.vtSymbol
                        t['OpenTime'] = entryTrade.tradeTime.strftime('%Y/%m/%d %H:%M:%S')
                        t['OpenPrice'] = entryTrade.price
                        t['Direction'] = u'Long'
                        t['CloseTime'] = trade.tradeTime.strftime('%Y/%m/%d %H:%M:%S')
                        t['ClosePrice'] = trade.price
                        t['Volume'] = tradeUnit
                        t['Profit'] = result.pnl
                        t['Commission'] = result.commission
                        self.exportTradeList.append(t)

                        self.writeCtaLog(u'{9}@{6} [{7}:开多{0},buy:{1}]-[{8}.平多{2},sell:{3},vol:{4}],净盈亏pnl={5}'
                                    .format(entryTrade.tradeTime, entryTrade.price,
                                            trade.tradeTime,trade.price, tradeUnit, result.pnl,
                                            i, longid, tradeid, gId))
                        i = i+1

                    if type(gr) != type(None):
                        self.writeCtaLog(u'组合净盈亏:{0}'.format(gr.pnl))

                    self.writeCtaLog(u'-------------')

        # 检查是否有交易
        if not resultDict:
            self.output(u'无交易结果')
            return {}
        
        # 然后基于每笔交易的结果，我们可以计算具体的盈亏曲线和最大回撤等

        """
        initCapital = 40000     # 期初资金
        capital = initCapital   # 资金
        maxCapital = initCapital          # 资金最高净值

        maxPnl = 0              # 最高盈利
        minPnl = 0              # 最大亏损
        maxVolume = 1             # 最大仓位数

        wins = 0

        totalResult = 0         # 总成交数量
        totalTurnover = 0       # 总成交金额（合约面值）
        totalCommission = 0     # 总手续费
        totalSlippage = 0       # 总滑点

        timeList = []           # 时间序列
        pnlList = []            # 每笔盈亏序列
        capitalList = []        # 盈亏汇总的时间序列
        drawdownList = []       # 回撤的时间序列
        drawdownRateList = []   # 最大回撤比例的时间序列
        """
        drawdown = 0            # 回撤
        compounding = 1        # 简单的复利基数（如果资金是期初资金的x倍，就扩大开仓比例,例如3w开1手，6w开2手，12w开4手)

        for time, result in resultDict.items():

            # 是否使用简单复利
            if self.usageCompounding:
                compounding = int(self.capital/self.initCapital)

            if result.pnl > 0:
                self.winningResult += 1
                self.totalWinning += result.pnl
            else:
                self.losingResult += 1
                self.totalLosing += result.pnl

            self.capital += result.pnl*compounding
            self.maxCapital = max(self.capital, self.maxCapital)        # 平仓后结算收益最大
            self.maxVolume = max(self.maxVolume, result.volume*compounding)
            drawdown = self.capital - self.maxCapital
            drawdownRate = round(float(drawdown*100/self.maxCapital),4)

            self.pnlList.append(result.pnl*compounding)
            self.timeList.append(time)
            self.capitalList.append(self.capningital)
            self.drawdownList.append(drawdown)
            self.drawdownRateList.append(drawdownRate)

            self.totalResult += 1
            self.totalTurnover += result.turnover*compounding
            self.totalCommission += result.commission*compounding
            self.totalSlippage += result.slippage*compounding

    # ---------------------------------------------------------------------
    def exportTradeResult(self):
        """
        导出回测结果表
        导出每日净值结果表
        :return:
        """
        if not self.exportTradeList:
            return
        s = EMPTY_STRING
        s = self.strategy_name.replace('&','')
        s = s.replace(' ', '')
        csvOutputFile = os.path.abspath(os.path.join(self.get_logs_path(),
                                                     '{}_TradeList_{}.csv'.format(s, datetime.now().strftime('%Y%m%d_%H%M'))))

        self.writeCtaLog(u'save trade records to:{}'.format(csvOutputFile))
        import csv
        csvWriteFile = open(csvOutputFile, 'w', encoding='utf8', newline='')

        fieldnames = ['Gid', 'vtSymbol','OpenTime', 'OpenPrice', 'Direction', 'CloseTime', 'ClosePrice', 'Volume', 'Profit', 'Commission']
        writer = csv.DictWriter(f=csvWriteFile, fieldnames=fieldnames, dialect='excel')
        writer.writeheader()

        for row in self.exportTradeList:
            writer.writerow(row)

        # 交易记录生成文华对应的公式
        if self.export_wenhua_signal:
            filename = os.path.abspath(os.path.join(self.get_logs_path(),
                                                    '{}_WenHua_{}.txt'.format(s, datetime.now().strftime('%Y%m%d_%H%M'))))
            self.writeCtaLog(u'save trade records for WenHua:{}'.format(filename))
            wenhuaSingalCount = 0
            wenhuaSignalFile = open(filename, mode='w')

            for t in self.exportTradeList:
                if t['Direction'] is 'Long':
                    # 生成文华用的指标信号
                    msg = 'Buy@{},{}'.format(t['OpenPrice'], t['Volume'])
                    self.writeWenHuaSignal(wenhuaSignalFile, wenhuaSingalCount, datetime.strptime(t['OpenTime'], '%Y-%m-%d %H:%M:%S'), t['OpenPrice'], msg)
                    wenhuaSingalCount += 1
                    msg = 'Sell@{},{} ({})'.format(t['ClosePrice'], t['Volume'], round(t['Profit']))
                    self.writeWenHuaSignal(wenhuaSignalFile, wenhuaSingalCount, datetime.strptime(t['CloseTime'], '%Y-%m-%d %H:%M:%S'), t['ClosePrice'], msg)
                    wenhuaSingalCount += 1
                else:
                    # 生成文华用的指标信号
                    msg = 'Short@{},{}'.format(t['OpenPrice'], t['Volume'])
                    self.writeWenHuaSignal(wenhuaSignalFile, wenhuaSingalCount, datetime.strptime(t['OpenTime'], '%Y-%m-%d %H:%M:%S'), t['OpenPrice'], msg)
                    wenhuaSingalCount += 1
                    msg = 'Cover@{},{} ({})'.format(t['ClosePrice'], t['Volume'], round(t['Profit']))
                    self.writeWenHuaSignal(wenhuaSignalFile, wenhuaSingalCount, datetime.strptime(t['CloseTime'], '%Y-%m-%d %H:%M:%S'), t['ClosePrice'], msg)
                    wenhuaSingalCount += 1
            wenhuaSignalFile.close()
#         wh_records = OrderedDict()
#         for t in self.exportTradeList:
#             if t['Direction'] is 'Long':
#                 k = '{}_{}_{}'.format(t['OpenTime'], 'Buy', t['OpenPrice'])
#                 # 生成文华用的指标信号
#                 v = {'time': datetime.strptime(t['OpenTime'], '%Y-%m-%d %H:%M:%S'), 'price':t['OpenPrice'], 'action': 'Buy', 'volume':t['Volume']}
#                 r = wh_records.get(k,None)
#                 if r is not None:
#                     r['volume'] += t['Volume']
#                 else:
#                     wh_records[k] = v
#
#                 k = '{}_{}_{}'.format(t['CloseTime'], 'Sell', t['ClosePrice'])
#                 # 生成文华用的指标信号
#                 v = {'time': datetime.strptime(t['CloseTime'], '%Y-%m-%d %H:%M:%S'), 'price': t['ClosePrice'], 'action': 'Sell', 'volume': t['Volume']}
#                 r = wh_records.get(k, None)
#                 if r is not None:
#                     r['volume'] += t['Volume']
#                 else:
#                     wh_records[k] = v
#
#             else:
#                 k = '{}_{}_{}'.format(t['OpenTime'], 'Short', t['OpenPrice'])
#                 # 生成文华用的指标信号
#                 v = {'time': datetime.strptime(t['OpenTime'], '%Y-%m-%d %H:%M:%S'), 'price': t['OpenPrice'], 'action': 'Short', 'volume': t['Volume']}
#                 r = wh_records.get(k, None)
#                 if r is not None:
#                     r['volume'] += t['Volume']
#                 else:
#                     wh_records[k] = v
#                 k = '{}_{}_{}'.format(t['CloseTime'], 'Cover', t['ClosePrice'])
#                 # 生成文华用的指标信号
#                 v = {'time': datetime.strptime(t['CloseTime'], '%Y-%m-%d %H:%M:%S'), 'price': t['ClosePrice'], 'action': 'Cover', 'volume': t['Volume']}
#                 r = wh_records.get(k, None)
#                 if r is not None:
#                     r['volume'] += t['Volume']
#                 else:
#                     wh_records[k] = v
#
#         branchs =  0
#         count = 0
#         wh_signal_file = None
#         for r in list(wh_records.values()):
#             if count % 200 == 0:
#                 if wh_signal_file is not None:
#                     wh_signal_file.close()
#
#                 # 交易记录生成文华对应的公式
#                 filename = os.path.abspath(os.path.join(self.get_logs_path(),
#                                                         '{}_WenHua_{}_{}.csv'.format(s, datetime.now().strftime('%Y%m%d_%H%M'), branchs)))
#                 branchs += 1
#                 self.writeCtaLog(u'save trade records for WenHua:{}'.format(filename))
#
#                 wh_signal_file = open(filename, mode='w')
#
#             count += 1
#             if wh_signal_file is not None:
#                 self.writeWenHuaSignal(filehandle=wh_signal_file, count=count, bardatetime=r['time'],price=r['price'], text='{}({})'.format(r['action'],r['volume']))
#         if wh_signal_file is not None:
#             wh_signal_file.close()

        # 导出每日净值记录表
        if not self.dailyList:
            return

        if self.daily_report_name == EMPTY_STRING:
            csvOutputFile2 = os.path.abspath(os.path.join(self.get_logs_path(),
                                         '{}_DailyList_{}.csv'.format(s, datetime.now().strftime('%Y%m%d_%H%M'))))
        else:
            csvOutputFile2 = self.daily_report_name
        self.writeCtaLog(u'save daily records to:{}'.format(csvOutputFile2))

        csvWriteFile2 = open(csvOutputFile2, 'w', encoding='utf8',newline='')
        fieldnames = ['date','lastPrice','capital','net','maxCapital','rate','commission','longMoney','shortMoney','occupyMoney','occupyRate','longPos','shortPos','todayMarginLong','todayMarginShort','benchmark']
        writer2 = csv.DictWriter(f=csvWriteFile2, fieldnames=fieldnames, dialect='excel')
        writer2.writeheader()

        for row in self.dailyList:
            writer2.writerow(row)

        return

    def getResult(self):
        # 返回回测结果
        d = {}
        d['initCapital'] = self.initCapital
        d['capital'] = self.capital - self.initCapital
        d['maxCapital'] = self.maxNetCapital    # 取消原 maxCapital

        if len(self.pnlList)  == 0:
            return {}, [], []

        d['maxPnl'] = max(self.pnlList)
        d['minPnl'] = min(self.pnlList)

        d['maxVolume'] = self.maxVolume
        d['totalResult'] = self.totalResult
        d['totalTurnover'] = self.totalTurnover
        d['totalCommission'] = self.totalCommission
        d['totalSlippage'] = self.totalSlippage
        d['timeList'] = self.timeList
        d['pnlList'] = self.pnlList
        d['capitalList'] = self.capitalList
        d['drawdownList'] = self.drawdownList
        d['drawdownRateList'] = self.drawdownRateList           #净值最大回撤率列表
        d['winningRate'] = round(100 * self.winningResult / len(self.pnlList), 4)

        averageWinning = 0  # 这里把数据都初始化为0
        averageLosing = 0
        profitLossRatio = 0

        if self.winningResult:
            averageWinning = self.totalWinning / self.winningResult  # 平均每笔盈利
        if self.losingResult:
            averageLosing = self.totalLosing / self.losingResult  # 平均每笔亏损
        if averageLosing:
            profitLossRatio = -averageWinning / averageLosing  # 盈亏比

        d['averageWinning'] = averageWinning
        d['averageLosing'] = averageLosing
        d['profitLossRatio'] = profitLossRatio

        # 计算Sharp
        if not self.dailyList:
            return

        capitalNetList = []
        capitalList = []
        for row in self.dailyList:
            capitalNetList.append(row['net'])
            capitalList.append(row['capital'])

        capital = pd.Series(capitalNetList)
        log_returns = np.log(capital).diff().fillna(0)
        sharpe = (log_returns.mean() * 252) / (log_returns.std() * np.sqrt(252))
        d['sharpe'] = sharpe

        return d, capitalNetList, capitalList

    #----------------------------------------------------------------------
    def showBacktestingResult(self):
        """显示回测结果"""
        if self.calculateMode != self.REALTIME_MODE:
            self.calculateBacktestingResult()

        d, dailyNetCapital, dailyCapital = self.getResult()

        if len(d) == 0:
            self.output(u'无交易结果')
            return

        # 导出交易清单
        self.exportTradeResult()

        # 输出
        self.writeCtaNotification('-' * 30)
        self.writeCtaNotification(u'第一笔交易：\t%s' % d['timeList'][0])
        self.writeCtaNotification(u'最后一笔交易：\t%s' % d['timeList'][-1])

        self.writeCtaNotification(u'总交易次数：\t%s' % formatNumber(d['totalResult']))
        self.writeCtaNotification(u'期初资金：\t%s' % formatNumber(d['initCapital']))
        self.writeCtaNotification(u'总盈亏：\t%s' % formatNumber(d['capital']))
        self.writeCtaNotification(u'资金最高净值：\t%s' % formatNumber(d['maxCapital']))
        self.writeCtaNotification(u'资金最高净值时间：\t%s' % self.maxNetCapital_time)

        self.writeCtaNotification(u'每笔最大盈利：\t%s' % formatNumber(d['maxPnl']))
        self.writeCtaNotification(u'每笔最大亏损：\t%s' % formatNumber(d['minPnl']))
        self.writeCtaNotification(u'净值最大回撤: \t%s' % formatNumber(min(d['drawdownList'])))
        #self.writeCtaNotification(u'净值最大回撤率: \t%s' % formatNumber(max(d['drawdownRateList'])))
        self.writeCtaNotification(u'净值最大回撤率: \t%s' % formatNumber(self.daily_max_drawdown_rate))
        self.writeCtaNotification(u'净值最大回撤时间：\t%s' % self.max_drowdown_rate_time)
        self.writeCtaNotification(u'胜率：\t%s' % formatNumber(d['winningRate']))

        self.writeCtaNotification(u'盈利交易平均值\t%s' % formatNumber(d['averageWinning']))
        self.writeCtaNotification(u'亏损交易平均值\t%s' % formatNumber(d['averageLosing']))
        self.writeCtaNotification(u'盈亏比：\t%s' % formatNumber(d['profitLossRatio']))

        self.writeCtaNotification(u'最大持仓：\t%s' % formatNumber(d['maxVolume']))

        self.writeCtaNotification(u'平均每笔盈利：\t%s' %formatNumber(d['capital']/d['totalResult']))

        self.writeCtaNotification(u'平均每笔滑点成本：\t%s' %formatNumber(d['totalSlippage']/d['totalResult']))
        self.writeCtaNotification(u'平均每笔佣金：\t%s' %formatNumber(d['totalCommission']/d['totalResult']))
        self.writeCtaNotification(u'Sharpe Ratio：\t%s' % formatNumber(d['sharpe']))

        # 绘图
        import matplotlib
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MultipleLocator, FormatStrFormatter
        import numpy as np
        matplotlib.rcParams['figure.figsize'] = (20.0, 10.0)

        try:
            import seaborn as sns       # 如果安装了seaborn则设置为白色风格
            sns.set_style('whitegrid')
        except ImportError:
            pass

        # 是否显示每日资金曲线
        isPlotDaily = False # DEBUG
        #isPlotDaily = True
        capitalStr = ''
        if isPlotDaily == True:
            daily_df = pd.DataFrame(self.dailyList)
            daily_df = daily_df.set_index('date')

            pCapital = plt.subplot(4, 1, 1)
            pCapital.set_ylabel("trade capital")
            pCapital.plot(d['capitalList'], color='r', lw=0.8)
            plt.title(u'{}: {}~{}({}) NetCapital={}({}), #Trading={}({}/day), TotalCommission={}, MaxLots={}({}), MDD={}%'.format(
                                                            self.symbol,
                                                            self.startDate, self.endDate, len(dailyNetCapital),
                                                            dailyNetCapital[-1], min(d['drawdownList']),
                                                            d['totalResult'], int(d['totalResult']/len(dailyNetCapital)),
                                                            d['totalCommission'],
                                                            d['maxVolume'], max(daily_df['occupyMoney']),
                                                            self.daily_max_drawdown_rate))
            pCapital.grid()
            capitalStr = '{}.{}'.format(round(dailyNetCapital[-1]), round(min(d['drawdownList'])))

            pDailyCapital = plt.subplot(4, 1, 3)
            pDailyCapital.set_ylabel("daily capital")
            pDailyCapital.plot(dailyCapital, color='b', lw=0.8, label='Capital')
            pDailyCapital.plot(dailyNetCapital, color='r', lw=1, label='NetCapital')
            # Change the label of X-Axes to date
            xt = pDailyCapital.get_xticks()
            interval = len(dailyNetCapital) / 10
            interval = round(interval) -1
            xt3 = list(range(-10, len(dailyNetCapital), interval))
            xt2 = [daily_df.index[int(i)] for i in xt3[1:]]
            xt2.insert(0,'')
            xt2.append('')
            pDailyCapital.set_xticks(xt3)
            pDailyCapital.set_xticklabels(xt2)
            pDailyCapital.grid()
            pDailyCapital.legend()

            pLastPrice = plt.subplot(4, 1, 2)
            pLastPrice.set_ylabel("daily lastprice")
            pLastPrice.plot(daily_df['lastPrice'], color='y', lw=1, label='Price')
            pLastPrice.set_xticks(xt3)
            pLastPrice.set_xticklabels(xt2)
            pLastPrice.grid()
            pLastPrice.legend()

            pOccupyRate = plt.subplot(4, 1, 4)
            pOccupyRate.set_ylabel("occupyMoney")
            index = np.arange(len(daily_df['occupyMoney']))
            pOccupyRate.bar(index, daily_df['occupyMoney'], 0.4, color='b')
            pOccupyRate.set_xticks(xt3)
            pOccupyRate.set_xticklabels(xt2)
            pOccupyRate.grid()

        else:
            pCapital = plt.subplot(4, 1, 1)
            pCapital.set_ylabel("capital")
            pCapital.plot(d['capitalList'], color='r', lw=0.8)

            plt.title(u'{}~{},{} backtest result '.format(self.startDate, self.endDate, self.strategy_name))

            pDD = plt.subplot(4, 1, 2)
            pDD.set_ylabel("DD")
            pDD.bar(range(len(d['drawdownList'])), d['drawdownList'], color='g')

            pPnl = plt.subplot(4, 1, 3)
            pPnl.set_ylabel("pnl")
            pPnl.hist(d['pnlList'], bins=50, color='c')
       
        plt.tight_layout()
        #plt.xticks(xindex, tradeTimeIndex, rotation=30)  # 旋转15

        fig_file_name = os.path.abspath(os.path.join(self.get_logs_path(),
                                                     '{}_plot_{}_{}.png'.format(self.strategy_name,
                                                          datetime.now().strftime('%Y%m%d_%H%M'), capitalStr)))


        fig = plt.gcf()
        fig.savefig(fig_file_name)
        self.output (u'图表保存至：{0}'.format(fig_file_name))
        #plt.show()
        plt.close()

    #----------------------------------------------------------------------
    def putStrategyEvent(self, name):
        """发送策略更新事件，回测中忽略"""
        pass

    #----------------------------------------------------------------------
    def runOptimization(self, strategyClass, optimizationSetting):
        """优化参数"""
        # 获取优化设置
        settingList = optimizationSetting.generateSetting()
        targetName = optimizationSetting.optimizeTarget

        # 检查参数设置问题
        if not settingList or not targetName:
            self.output(u'优化设置有问题，请检查')

        # 遍历优化
        resultList = []
        for setting in settingList:
            self.clearBacktestingResult()
            self.output('-' * 30)
            self.output('setting: %s' %str(setting))
            self.initStrategy(strategyClass, setting)
            self.runBacktesting()
            d = self.calculateBacktestingResult()
            try:
                targetValue = d[targetName]
            except KeyError:
                targetValue = 0
            resultList.append(([str(setting)], targetValue))

        # 显示结果
        resultList.sort(reverse=True, key=lambda result:result[1])
        self.output('-' * 30)
        self.output(u'优化结果：')
        for result in resultList:
            self.output(u'%s: %s' %(result[0], result[1]))

        return resultList

    #----------------------------------------------------------------------
    def clearBacktestingResult(self):
        """清空之前回测的结果"""
        # 清空限价单相关
        self.limitOrderCount = 0
        self.limitOrderDict.clear()
        self.workingLimitOrderDict.clear()

        # 清空停止单相关
        self.stopOrderCount = 0
        self.stopOrderDict.clear()
        self.workingStopOrderDict.clear()

        # 清空成交相关
        self.tradeCount = 0
        self.tradeDict.clear()

    #----------------------------------------------------------------------
    def runParallelOptimization(self, strategyClass, optimizationSetting):
        """并行优化参数"""
        # 获取优化设置
        settingList = optimizationSetting.generateSetting()
        targetName = optimizationSetting.optimizeTarget

        # 检查参数设置问题
        if not settingList or not targetName:
            self.output(u'优化设置有问题，请检查')

        # 多进程优化，启动一个对应CPU核心数量的进程池
        pool = multiprocessing.Pool(multiprocessing.cpu_count())
        l = []

        for setting in settingList:
            l.append(pool.apply_async(optimize, (strategyClass, setting,
                                                 targetName, self.mode,
                                                 self.startDate, self.initDays, self.endDate,
                                                 self.slippage, self.rate, self.size,
                                                 self.dbName, self.symbol)))
        pool.close()
        pool.join()

        # 显示结果
        resultList = [res.get() for res in l]
        resultList.sort(reverse=True, key=lambda result:result[1])
        self.output('-' * 30)
        self.output(u'优化结果：')
        for result in resultList:
            self.output(u'%s: %s' %(result[0], result[1]))

    #----------------------------------------------------------------------
    def roundToPriceTick(self, price, priceTick=None):
        """取整价格到合约最小价格变动"""
        if not priceTick:
            priceTick = self.priceTick
        if not priceTick:
            return price

        newPrice = round(price/priceTick, 0) * priceTick
        return newPrice


    def roundToVolumeTick(self,volumeTick,volume):
        if volumeTick == 0:
            return volume
        newVolume = round(volume / volumeTick, 0) * volumeTick
        if isinstance(volumeTick,float):
            v_exponent = decimal.Decimal(str(newVolume))
            vt_exponent = decimal.Decimal(str(volumeTick))
            if abs(v_exponent.as_tuple().exponent) > abs(vt_exponent.as_tuple().exponent):
                newVolume = round(newVolume, ndigits=abs(vt_exponent.as_tuple().exponent))
                newVolume = float(str(newVolume))

        return newVolume

    def getTradingDate(self, dt=None):
        """
        根据输入的时间，返回交易日的日期
        :param dt:
        :return:
        """
        tradingDay = ''
        if dt is None:
            dt = datetime.now()

        if dt.hour >= 21:
            if dt.isoweekday() == 5:
                # 星期五=》星期一
                return (dt + timedelta(days=3)).strftime('%Y-%m-%d')
            else:
                # 第二天
                return (dt + timedelta(days=1)).strftime('%Y-%m-%d')
        elif dt.hour < 8 and dt.isoweekday() == 6:
            # 星期六=>星期一
            return (dt + timedelta(days=2)).strftime('%Y-%m-%d')
        else:
            return dt.strftime('%Y-%m-%d')

########################################################################
class TradingResult(object):
    """每笔交易的结果"""

    #----------------------------------------------------------------------
    def __init__(self, entryPrice,entryDt, exitPrice,exitDt,volume, rate, slippage, size, groupId, fixcommission=EMPTY_FLOAT):
        """Constructor"""
        self.entryPrice = entryPrice    # 开仓价格
        self.exitPrice = exitPrice      # 平仓价格

        self.entryDt = entryDt          # 开仓时间datetime
        self.exitDt = exitDt            # 平仓时间

        self.volume = volume    # 交易数量（+/-代表方向）
        self.groupId = groupId  # 主交易ID（针对多手平仓）

        self.turnover = (self.entryPrice + self.exitPrice) * size * abs(volume)  # 成交金额
        if fixcommission:
            self.commission = fixcommission * abs(self.volume)
        else:
            self.commission = abs(self.turnover * rate)  # 手续费成本
        self.slippage = slippage * 2 * size * abs(volume)  # 滑点成本
        self.pnl = ((self.exitPrice - self.entryPrice) * volume * size
                    - self.commission - self.slippage)  # 净盈亏


########################################################################
class OptimizationSetting(object):
    """优化设置"""

    #----------------------------------------------------------------------
    def __init__(self):
        """Constructor"""
        self.paramDict = OrderedDict()

        self.optimizeTarget = ''        # 优化目标字段

    #----------------------------------------------------------------------
    def addParameter(self, name, start, end=None, step=None):
        """增加优化参数"""
        if end is None and step is None:
            self.paramDict[name] = [start]
            return

        if end < start:
            print( u'参数起始点必须不大于终止点')
            return

        if step <= 0:
            print( u'参数布进必须大于0')
            return

        l = []
        param = start

        while param <= end:
            l.append(param)
            param += step

        self.paramDict[name] = l

    #----------------------------------------------------------------------
    def generateSetting(self):
        """生成优化参数组合"""
        # 参数名的列表
        nameList = list(self.paramDict.keys())
        paramList = list(self.paramDict.values())

        # 使用迭代工具生产参数对组合
        productList = list(product(*paramList))

        # 把参数对组合打包到一个个字典组成的列表中
        settingList = []
        for p in productList:
            d = dict(zip(nameList, p))
            settingList.append(d)

        return settingList

    #----------------------------------------------------------------------
    def setOptimizeTarget(self, target):
        """设置优化目标字段"""
        self.optimizeTarget = target

#----------------------------------------------------------------------
def formatNumber(n):
    """格式化数字到字符串"""
    rn = round(n, 2)        # 保留两位小数
    return format(rn, ',')  # 加上千分符


#----------------------------------------------------------------------
def optimize(strategyClass, setting, targetName,
             mode, startDate, initDays, endDate,
             slippage, rate, size,
             dbName, symbol):
    """多进程优化时跑在每个进程中运行的函数"""
    engine = BacktestingEngine()
    engine.setBacktestingMode(mode)
    engine.setStartDate(startDate, initDays)
    engine.setEndDate(endDate)
    engine.setSlippage(slippage)
    engine.setRate(rate)
    engine.setSize(size)
    engine.setDatabase(dbName, symbol)

    engine.initStrategy(strategyClass, setting)
    engine.runBacktesting()
    d = engine.calculateBacktestingResult()
    try:
        targetValue = d[targetName]
    except KeyError:
        targetValue = 0
    return (str(setting), targetValue)


    
    