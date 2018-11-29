# encoding: UTF-8

'''
vn.ctp的gateway接入

考虑到现阶段大部分CTP中的ExchangeID字段返回的都是空值
vtSymbol直接使用symbol
'''
print('loading ctpGateway.py')
import os
import json

# 加载经booster编译转换的SO API库
from vnpy.trader.gateway.ctpGateway.vnctpmd import MdApi
from vnpy.trader.gateway.ctpGateway.vnctptd import TdApi
print(u'loaded vnctpmd/vnctptd')
from vnpy.trader.vtConstant import  *
from vnpy.trader.vtGateway import *
from vnpy.trader.gateway.ctpGateway.language import text
from vnpy.trader.gateway.ctpGateway.ctpDataType import *
from vnpy.trader.vtFunction import getJsonPath
from datetime import datetime,timedelta

# 以下为一些VT类型和CTP类型的映射字典
# 价格类型映射
priceTypeMap = {}
priceTypeMap[PRICETYPE_LIMITPRICE] = defineDict["THOST_FTDC_OPT_LimitPrice"]
priceTypeMap[PRICETYPE_MARKETPRICE] = defineDict["THOST_FTDC_OPT_AnyPrice"]
priceTypeMapReverse = {v: k for k, v in priceTypeMap.items()} 

# 方向类型映射
directionMap = {}
directionMap[DIRECTION_LONG] = defineDict['THOST_FTDC_D_Buy']
directionMap[DIRECTION_SHORT] = defineDict['THOST_FTDC_D_Sell']
directionMapReverse = {v: k for k, v in directionMap.items()}

# 开平类型映射
offsetMap = {}
offsetMap[OFFSET_OPEN] = defineDict['THOST_FTDC_OF_Open']
offsetMap[OFFSET_CLOSE] = defineDict['THOST_FTDC_OF_Close']
offsetMap[OFFSET_CLOSETODAY] = defineDict['THOST_FTDC_OF_CloseToday']
offsetMap[OFFSET_CLOSEYESTERDAY] = defineDict['THOST_FTDC_OF_CloseYesterday']
offsetMapReverse = {v:k for k,v in offsetMap.items()}

# 交易所类型映射
exchangeMap = {}
exchangeMap[EXCHANGE_CFFEX] = 'CFFEX'
exchangeMap[EXCHANGE_SHFE] = 'SHFE'
exchangeMap[EXCHANGE_CZCE] = 'CZCE'
exchangeMap[EXCHANGE_DCE] = 'DCE'
exchangeMap[EXCHANGE_SSE] = 'SSE'
exchangeMap[EXCHANGE_SZSE] = 'SZSE'
exchangeMap[EXCHANGE_INE] = 'INE'
exchangeMap[EXCHANGE_UNKNOWN] = ''
exchangeMapReverse = {v:k for k,v in exchangeMap.items()}

# 持仓类型映射
posiDirectionMap = {}
posiDirectionMap[DIRECTION_NET] = defineDict["THOST_FTDC_PD_Net"]
posiDirectionMap[DIRECTION_LONG] = defineDict["THOST_FTDC_PD_Long"]
posiDirectionMap[DIRECTION_SHORT] = defineDict["THOST_FTDC_PD_Short"]
posiDirectionMapReverse = {v:k for k,v in posiDirectionMap.items()}

# 产品类型映射
productClassMap = {}
productClassMap[PRODUCT_FUTURES] = defineDict["THOST_FTDC_PC_Futures"]
productClassMap[PRODUCT_OPTION] = defineDict["THOST_FTDC_PC_Options"]
productClassMap[PRODUCT_COMBINATION] = defineDict["THOST_FTDC_PC_Combination"]
productClassMapReverse = {v:k for k,v in productClassMap.items()}
productClassMapReverse[defineDict["THOST_FTDC_PC_ETFOption"]] = PRODUCT_OPTION
productClassMapReverse[defineDict["THOST_FTDC_PC_Stock"]] = PRODUCT_EQUITY

# 委托状态映射
statusMap = {}
statusMap[STATUS_ALLTRADED] = defineDict["THOST_FTDC_OST_AllTraded"]
statusMap[STATUS_PARTTRADED] = defineDict["THOST_FTDC_OST_PartTradedQueueing"]
statusMap[STATUS_NOTTRADED] = defineDict["THOST_FTDC_OST_NoTradeQueueing"]
statusMap[STATUS_CANCELLED] = defineDict["THOST_FTDC_OST_Canceled"]
statusMapReverse = {v:k for k,v in statusMap.items()}

# 全局字典, key:symbol, value:exchange
symbolExchangeDict = {}

# 夜盘交易时间段分隔判断
NIGHT_TRADING = datetime(1900, 1, 1, 20).time()


########################################################################
class CtpGateway(VtGateway):
    """CTP接口"""

    #----------------------------------------------------------------------
    def __init__(self, eventEngine, gatewayName='CTP'):
        """Constructor"""
        super(CtpGateway, self).__init__(eventEngine, gatewayName)

        self.mdApi = None     # 行情API
        self.tdApi = None     # 交易API

        self.mdConnected = False        # 行情API连接状态，登录完成后为True
        self.tdConnected = False        # 交易API连接状态
        
        self.qryEnabled = False         # 是否要启动循环查询

        self.subscribedSymbols = set()  # 已订阅合约代码
        self.requireAuthentication = False
        
    #----------------------------------------------------------------------
    def connect(self):
        """连接"""
        # 载入json文件
        fileName = self.gatewayName + '_connect.json'
        filePath = getJsonPath(fileName, __file__)

        if self.mdApi is None:
            self.writeLog(u'行情接口未实例化，创建实例')
            self.mdApi = CtpMdApi(self)     # 行情API
        else:
            self.writeLog(u'行情接口已实例化')

        if self.tdApi is None:
            self.writeLog(u'交易接口未实例化，创建实例')
            self.tdApi = CtpTdApi(self)     # 交易API
        else:
            self.writeLog(u'交易接口已实例化')

        setting = None
        try:
            with open(filePath,'r') as f:
                # 解析json文件
                setting = json.load(f)
        except IOError:
            self.writeLog('{} {}'.format(filePath,text.LOADING_ERROR))
            return

        try:
            userID = str(setting['userID'])
            password = str(setting['password'])
            brokerID = str(setting['brokerID'])
            tdAddress = str(setting['tdAddress'])
            mdAddress = str(setting['mdAddress'])

            # 如果json文件提供了验证码
            if 'authCode' in setting:
                authCode = str(setting['authCode'])
                userProductInfo = str(setting['userProductInfo'])
                self.tdApi.requireAuthentication = True
            else:
                authCode = None
                userProductInfo = None

        except KeyError:
            self.writeLog(text.CONFIG_KEY_MISSING)
            return
        
        # 创建行情和交易接口对象
        self.writeLog(u'连接行情服务器')
        self.mdApi.connect(userID, password, brokerID, mdAddress)
        self.writeLog(u'连接交易服务器')
        self.tdApi.connect(userID, password, brokerID, tdAddress, authCode, userProductInfo)
        self.setQryEnabled(True)
        # 初始化并启动查询
        self.initQuery()

        for req in self.subscribedSymbols:
            self.mdApi.subscribe(req)
    
    #----------------------------------------------------------------------
    def subscribe(self, subscribeReq):
        """订阅行情"""
        if self.mdApi is not None:
            self.mdApi.subscribe(subscribeReq)

        # Allow the strategies to start before the connection
        self.subscribedSymbols.add(subscribeReq)
        
    #----------------------------------------------------------------------
    def sendOrder(self, orderReq):
        """发单"""
        if self.tdApi is not None:
            return self.tdApi.sendOrder(orderReq)
        
    #----------------------------------------------------------------------
    def cancelOrder(self, cancelOrderReq):
        """撤单"""
        if self.tdApi is not None:
            self.tdApi.cancelOrder(cancelOrderReq)
        
    #----------------------------------------------------------------------
    def qryAccount(self):
        """查询账户资金"""
        if self.tdApi is None:
            self.tdConnected = False
            return
        self.tdApi.qryAccount()

    #----------------------------------------------------------------------
    def qryPosition(self):
        """查询持仓"""
        if self.tdApi is None:
            self.mdConnected = False
            return
        self.tdApi.qryPosition()

    def checkStatus(self):
        """查询md/td的状态"""
        if self.tdApi is None or self.mdApi is None:
            return False

        if not self.tdConnected or not self.mdConnected:
            return False

        return True
    #----------------------------------------------------------------------
    def close(self):
        """关闭"""
        if self.mdApi is not None:
            self.writeLog(u'断开行情API')
            tmp1 = self.mdApi
            self.mdApi = None
            tmp1.close()
            self.mdConnected = False

        if self.tdApi is not None:
            self.writeLog(u'断开交易API')
            tmp2 = self.tdApi
            self.tdApi = None
            tmp2.close()
            self.tdConnected = False

        self.writeLog(u'主动断开连接')
        
    #----------------------------------------------------------------------
    def initQuery(self):
        """初始化连续查询"""
        if self.qryEnabled:
            # 需要循环的查询函数列表
            self.qryFunctionList = [self.qryAccount, self.qryPosition]
            
            self.qryCount = 0           # 查询触发倒计时
            self.qryTrigger = 2         # 查询触发点
            self.qryNextFunction = 0    # 上次运行的查询函数索引
            
            self.startQuery()
    
    #----------------------------------------------------------------------
    def query(self, event):
        """注册到事件处理引擎上的查询函数"""
        self.qryCount += 1
        
        if self.qryCount > self.qryTrigger:
            # 清空倒计时
            self.qryCount = 0
            
            # 执行查询函数
            function = self.qryFunctionList[self.qryNextFunction]
            function()
            
            # 计算下次查询函数的索引，如果超过了列表长度，则重新设为0
            self.qryNextFunction += 1
            if self.qryNextFunction == len(self.qryFunctionList):
                self.qryNextFunction = 0
    
    #----------------------------------------------------------------------
    def startQuery(self):
        """启动连续查询"""
        self.eventEngine.register(EVENT_TIMER, self.query)
    
    #----------------------------------------------------------------------
    def setQryEnabled(self, qryEnabled):
        """设置是否要启动循环查询"""
        self.qryEnabled = qryEnabled

    # ----------------------------------------------------------------------
    def writeLog(self, content):
        """发出日志"""
        log = VtLogData()
        log.gatewayName = self.gatewayName
        log.logContent = content
        self.onLog(log)


########################################################################
class CtpMdApi(MdApi):
    """CTP行情API实现"""

    #----------------------------------------------------------------------
    def __init__(self, gateway):
        """Constructor"""
        super(CtpMdApi, self).__init__()
        
        self.gateway = gateway                  # gateway对象
        self.gatewayName = gateway.gatewayName  # gateway对象名称
        
        self.reqID = EMPTY_INT              # 操作请求编号
        
        self.connectionStatus = False       # 连接状态
        self.loginStatus = False            # 登录状态
        
        self.subscribedSymbols = gateway.subscribedSymbols     # 已订阅合约代码
        
        self.userID = EMPTY_STRING          # 账号
        self.password = EMPTY_STRING        # 密码
        self.brokerID = EMPTY_STRING        # 经纪商代码
        self.address = EMPTY_STRING         # 服务器地址
        
    #----------------------------------------------------------------------
    def onFrontConnected(self):
        """服务器连接"""
        self.connectionStatus = True
        
        self.writeLog(text.DATA_SERVER_CONNECTED)

        self.login()
    
    #----------------------------------------------------------------------  
    def onFrontDisconnected(self, n):
        """服务器断开"""
        self.connectionStatus = False
        self.loginStatus = False
        self.gateway.mdConnected = False
        
        self.writeLog(text.DATA_SERVER_DISCONNECTED)
        
    #---------------------------------------------------------------------- 
    def onHeartBeatWarning(self, n):
        """心跳报警"""
        # 因为API的心跳报警比较常被触发，且与API工作关系不大，因此选择忽略
        pass
    
    #----------------------------------------------------------------------   
    def onRspError(self, error, n, last):
        """错误回报"""
        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = error['ErrorMsg']    #.decode('gbk')
        self.gateway.onError(err)
        
    #----------------------------------------------------------------------
    def onRspUserLogin(self, data, error, n, last):
        """登陆回报"""
        # 如果登录成功，推送日志信息
        if error['ErrorID'] == 0:
            self.loginStatus = True
            self.gateway.mdConnected = True
            self.writeLog(text.DATA_SERVER_LOGIN)
            # 重新订阅之前订阅的合约

            for subscribeReq in self.subscribedSymbols:
                self.subscribe(subscribeReq)
                
        # 否则，推送错误信息
        else:
            err = VtErrorData()
            err.gatewayName = self.gatewayName
            err.errorID = error['ErrorID']
            err.errorMsg = error['ErrorMsg']    #.decode('gbk')
            self.gateway.onError(err)

    #---------------------------------------------------------------------- 
    def onRspUserLogout(self, data, error, n, last):
        """登出回报"""
        # 如果登出成功，推送日志信息
        if error['ErrorID'] == 0:
            self.loginStatus = False
            self.gateway.mdConnected = False
            
            self.writeLog(text.DATA_SERVER_LOGOUT)
                
        # 否则，推送错误信息
        else:
            err = VtErrorData()
            err.gatewayName = self.gatewayName
            err.errorID = error['ErrorID']
            err.errorMsg = error['ErrorMsg']    #.decode('gbk')
            self.gateway.onError(err)
        
    #----------------------------------------------------------------------  
    def onRspSubMarketData(self, data, error, n, last):
        """订阅合约回报"""
        # 通常不在乎订阅错误，选择忽略
        pass
        
    #----------------------------------------------------------------------  
    def onRspUnSubMarketData(self, data, error, n, last):
        """退订合约回报"""
        # 同上
        pass  
        
    #----------------------------------------------------------------------  
    def onRtnDepthMarketData(self, data):
        """行情推送"""
        # 忽略成交量为0的无效单合约tick数据
        #if not data['Volume'] and '&' not in data['InstrumentID']:
        #    self.writeLog(u'忽略成交量为0的无效单合约tick数据:')
        #    self.writeLog(data)
        #    return

        if not self.connectionStatus:
            self.connectionStatus = True

        if not self.gateway.mdConnected:
            self.gateway.mdConnected = True

        tick = VtTickData()
        tick.gatewayName = self.gatewayName
        
        tick.symbol = data['InstrumentID']
        tick.exchange = exchangeMapReverse.get(data['ExchangeID'], u'未知')
        tick.vtSymbol = tick.symbol #'.'.join([tick.symbol, EXCHANGE_UNKNOWN])
        
        tick.lastPrice = data['LastPrice']
        tick.volume = data['Volume']
        tick.openInterest = data['OpenInterest']
        #tick.time = '.'.join([data['UpdateTime'], str(data['UpdateMillisec']/100)])
        # =》 Python 3
        tick.time = '.'.join([data['UpdateTime'], str(data['UpdateMillisec'])])

        # 取当前时间
        dt = datetime.now()

        # 不处理开盘前的tick数据
        if dt.hour in [8,20] and dt.minute < 59:
            return
        if tick.exchange is EXCHANGE_CFFEX and dt.hour ==9 and dt.minute < 14:
            return

        # 日期，取系统时间的日期
        tick.date = dt.strftime('%Y-%m-%d')
        # 生成dateteime
        tick.datetime = datetime.strptime(tick.date + ' ' + tick.time, '%Y-%m-%d %H:%M:%S.%f')
        # 生成TradingDay

        # 正常日盘时间
        tick.tradingDay = tick.date

        # 修正夜盘的tradingDay
        if tick.datetime.hour >= 20:
            # 周一~周四晚上20点之后的tick，交易日属于第二天
            if tick.datetime.isoweekday() in [1,2,3,4]:
                trading_day = tick.datetime + timedelta(days=1)
                tick.tradingDay = trading_day.strftime('%Y-%m-%d')
            # 周五晚上20点之后的tick，交易日属于下周一
            elif tick.datetime.isoweekday() == 5:
                trading_day = tick.datetime + timedelta(days=3)
                tick.tradingDay = trading_day.strftime('%Y-%m-%d')
        elif tick.datetime.hour < 3:
            # 周六凌晨的tick，交易日属于下周一
            if tick.datetime.isoweekday() == 6:
                trading_day = tick.datetime + timedelta(days=2)
                tick.tradingDay = trading_day.strftime('%Y-%m-%d')

        tick.openPrice = data['OpenPrice']
        tick.highPrice = data['HighestPrice']
        tick.lowPrice = data['LowestPrice']
        tick.preClosePrice = data['PreClosePrice']
        
        tick.upperLimit = data['UpperLimitPrice']
        tick.lowerLimit = data['LowerLimitPrice']
        
        # CTP只有一档行情
        tick.bidPrice1 = data['BidPrice1']
        tick.bidVolume1 = data['BidVolume1']
        tick.askPrice1 = data['AskPrice1']
        tick.askVolume1 = data['AskVolume1']

        self.gateway.onTick(tick)
        
    #---------------------------------------------------------------------- 
    def onRspSubForQuoteRsp(self, data, error, n, last):
        """订阅期权询价"""
        pass
        
    #----------------------------------------------------------------------
    def onRspUnSubForQuoteRsp(self, data, error, n, last):
        """退订期权询价"""
        pass 
        
    #---------------------------------------------------------------------- 
    def onRtnForQuoteRsp(self, data):
        """期权询价推送"""
        pass        
        
    #----------------------------------------------------------------------
    def connect(self, userID, password, brokerID, address):
        """初始化连接"""
        self.userID = userID                # 账号
        self.password = password            # 密码
        self.brokerID = brokerID            # 经纪商代码
        self.address = address              # 服务器地址
        
        # 如果尚未建立服务器连接，则进行连接
        if not self.connectionStatus:
            # 创建C++环境中的API对象，这里传入的参数是需要用来保存.con文件的文件夹路径
            path = os.getcwd() + '/temp/' + str(self.gatewayName) + '/'
            if not os.path.exists(path):
                os.makedirs(path)
            self.createFtdcMdApi(path)
            
            # 注册服务器地址
            self.registerFront(self.address)
            
            # 初始化连接，成功会调用onFrontConnected
            self.init()
            
        # 若已经连接但尚未登录，则进行登录
        else:
            if not self.loginStatus:
                self.login()
        
    #----------------------------------------------------------------------
    def subscribe(self, subscribeReq):
        """订阅合约"""
        # 这里的设计是，如果尚未登录就调用了订阅方法
        # 则先保存订阅请求，登录完成后会自动订阅
        #if self.loginStatus:
        print(u'subscribe {0}'.format(str(subscribeReq.symbol)))
        self.subscribeMarketData(str(subscribeReq.symbol))
        self.writeLog(u'订阅合约:{0}'.format(str(subscribeReq.symbol)))
        #else:
        #    print u'not login, add {0} into subscribe list'.format(str(subscribeReq.symbol))
        #    self.writeLog(u'未连接，增加合约{0}至待订阅列表'.format(str(subscribeReq.symbol)))

        self.subscribedSymbols.add(subscribeReq)   
        
    #----------------------------------------------------------------------
    def login(self):
        """登录"""
        # 如果填入了用户名密码等，则登录
        if self.userID and self.password and self.brokerID:
            req = {}
            req['UserID'] = self.userID
            req['Password'] = self.password
            req['BrokerID'] = self.brokerID
            self.reqID += 1
            self.reqUserLogin(req, self.reqID)    
    
    #----------------------------------------------------------------------
    def close(self):
        """关闭"""
        self.exit()

    #----------------------------------------------------------------------
    def writeLog(self, content):
        """发出日志"""
        log = VtLogData()
        log.gatewayName = self.gatewayName
        log.logContent = content
        self.gateway.onLog(log)


########################################################################
class CtpTdApi(TdApi):
    """CTP交易API实现"""
    
    #----------------------------------------------------------------------
    def __init__(self, gateway):
        """API对象的初始化函数"""
        super(CtpTdApi, self).__init__()
        
        self.gateway = gateway                  # gateway对象
        self.gatewayName = gateway.gatewayName  # gateway对象名称
        
        self.reqID = EMPTY_INT              # 操作请求编号
        self.orderRef = EMPTY_INT           # 订单编号
        
        self.connectionStatus = False       # 连接状态
        self.loginStatus = False            # 登录状态
        self.authStatus = False

        self.userID = EMPTY_STRING          # 账号
        self.password = EMPTY_STRING        # 密码
        self.brokerID = EMPTY_STRING        # 经纪商代码
        self.address = EMPTY_STRING         # 服务器地址
        
        self.frontID = EMPTY_INT            # 前置机编号
        self.sessionID = EMPTY_INT          # 会话编号
        
        self.posDict = {}
        self.symbolExchangeDict = {}        # 保存合约代码和交易所的印射关系
        self.symbolSizeDict = {}            # 保存合约代码和合约大小的印射关系

        self.requireAuthentication = False

        self.tradingDay = None

    #----------------------------------------------------------------------
    def onFrontConnected(self):
        """服务器连接"""
        self.connectionStatus = True
        
        self.writeLog(text.TRADING_SERVER_CONNECTED)
        
        if self.requireAuthentication:
            self.authenticate()
        else:
            self.login()
    
    #----------------------------------------------------------------------
    def onFrontDisconnected(self, n):
        """服务器断开"""
        self.connectionStatus = False
        self.loginStatus = False
        self.gateway.tdConnected = False
        
        self.writeLog(text.TRADING_SERVER_DISCONNECTED)
    
    #----------------------------------------------------------------------
    def onHeartBeatWarning(self, n):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspAuthenticate(self, data, error, n, last):
        """验证客户端回报"""
        if error['ErrorID'] == 0:
            self.authStatus = True

            self.writeLog(text.TRADING_SERVER_AUTHENTICATED)

            self.login()
    
    #----------------------------------------------------------------------
    def onRspUserLogin(self, data, error, n, last):
        """登陆回报"""
        # 如果登录成功，推送日志信息
        if error['ErrorID'] == 0:
            self.tradingDay = str(data['TradingDay'])
            self.frontID = str(data['FrontID'])
            self.sessionID = str(data['SessionID'])
            self.loginStatus = True
            self.gateway.tdConnected = True
            self.gateway.mdConnected = True
            self.writeLog(text.TRADING_SERVER_LOGIN)

            # 确认结算信息
            req = {}
            req['BrokerID'] = self.brokerID
            req['InvestorID'] = self.userID
            self.reqID += 1
            self.reqSettlementInfoConfirm(req, self.reqID)

            # 提交合约更新请求
            try:
                self.resentReqQryInstrument()
            except:
                pass
                
        # 否则，推送错误信息
        else:
            err = VtErrorData()
            err.gatewayName = self.gatewayName
            err.errorID = error['ErrorID']
            err.errorMsg = error['ErrorMsg']    #.decode('gbk')
            self.gateway.onError(err)

    def resentReqQryInstrument(self):
        # 查询合约代码
        self.reqID += 1
        self.reqQryInstrument({}, self.reqID)

    #----------------------------------------------------------------------
    def onRspUserLogout(self, data, error, n, last):
        """登出回报"""
        # 如果登出成功，推送日志信息
        if error['ErrorID'] == 0:
            self.loginStatus = False
            self.gateway.tdConnected = False
            
            self.writeLog(text.TRADING_SERVER_LOGOUT)
                
        # 否则，推送错误信息
        else:
            err = VtErrorData()
            err.gatewayName = self.gatewayName
            err.errorID = error['ErrorID']
            err.errorMsg = error['ErrorMsg']    #.decode('gbk')
            self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onRspUserPasswordUpdate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspTradingAccountPasswordUpdate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspOrderInsert(self, data, error, n, last):
        """发单错误（柜台）"""
        # 推送委托信息
        order = VtOrderData()
        order.gatewayName = self.gatewayName
        order.symbol = data['InstrumentID']
        order.exchange = exchangeMapReverse[data['ExchangeID']]
        order.vtSymbol = order.symbol
        order.orderID = data['OrderRef']
        order.vtOrderID = '.'.join([self.gatewayName, order.orderID])
        order.direction = directionMapReverse.get(data['Direction'], DIRECTION_UNKNOWN)
        order.offset = offsetMapReverse.get(data['CombOffsetFlag'], OFFSET_UNKNOWN)
        order.status = STATUS_REJECTED
        order.price = data['LimitPrice']
        order.totalVolume = data['VolumeTotalOriginal']
        self.gateway.onOrder(order)

        # 推送错误信息
        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = error['ErrorMsg']    #.decode('gbk')
        err.additionalInfo = u'onRspOrderInsert():{0},{1},{2},{3}'.\
            format(order.vtSymbol, order.orderID, order.direction , order.offset)
        self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onRspParkedOrderInsert(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspParkedOrderAction(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspOrderAction(self, data, error, n, last):
        """撤单错误（柜台）"""
        try:
            symbol = data['InstrumentID']
        except KeyError:
            symbol = u'KEYERROR'

        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = error['ErrorMsg']    #.decode('gbk')
        err.additionalInfo = u'onRspOrderAction,{0}'.format(symbol)
        self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onRspQueryMaxOrderVolume(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspSettlementInfoConfirm(self, data, error, n, last):
        """确认结算信息回报"""
        self.writeLog(text.SETTLEMENT_INFO_CONFIRMED)

        
    #----------------------------------------------------------------------
    def onRspRemoveParkedOrder(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspRemoveParkedOrderAction(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspExecOrderInsert(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspExecOrderAction(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspForQuoteInsert(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQuoteInsert(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQuoteAction(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspLockInsert(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspCombActionInsert(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryOrder(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryTrade(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInvestorPosition(self, data, error, n, last):
        """持仓查询回报"""

        if not data['InstrumentID']:
            return

        if not self.gateway.tdConnected:
            self.gateway.tdConnected = True

        # 获取持仓缓存对象
        posName = '.'.join([data['InstrumentID'], data['PosiDirection']])
        if posName in self.posDict:
            pos = self.posDict[posName]
        else:
            pos = VtPositionData()
            self.posDict[posName] = pos
        
            pos.gatewayName = self.gatewayName
            pos.symbol = data['InstrumentID']
            pos.vtSymbol = pos.symbol
            pos.direction = posiDirectionMapReverse.get(data['PosiDirection'], '')
            pos.vtPositionName = '.'.join([pos.vtSymbol, pos.direction, pos.gatewayName])

        # 针对上期所持仓的今昨分条返回（有昨仓、无今仓），读取昨仓数据
        if data['YdPosition'] and not data['TodayPosition']:
            pos.ydPosition = data['Position']

        # 计算成本
        cost = pos.price * pos.position

        # 汇总总仓
        pos.position += data['Position']
        pos.positionProfit += data['PositionProfit']

        # 计算持仓均价
        if pos.position and pos.symbol in self.symbolSizeDict:
            size = self.symbolSizeDict[pos.symbol]
            if size > 0 and pos.position > 0:
                pos.price = (cost + data['PositionCost']) / abs(pos.position * size)

        # 读取冻结
        if pos.direction is DIRECTION_LONG:
            pos.frozen += data['LongFrozen']
        else:
            pos.frozen += data['ShortFrozen']

        # 查询回报结束
        if last:
            # 遍历推送
            for pos in list(self.posDict.values()):
                self.gateway.onPosition(pos)
    
            # 清空缓存
            self.posDict.clear()

    #----------------------------------------------------------------------
    def onRspQryTradingAccount(self, data, error, n, last):
        """资金账户查询回报"""
        self.gateway.mdConnected = True

        account = VtAccountData()
        account.gatewayName = self.gatewayName
        
        # 账户代码
        account.accountID = data['AccountID']
        account.vtAccountID = '.'.join([self.gatewayName, account.accountID])
        
        # 数值相关
        account.preBalance = data['PreBalance']
        account.available = data['Available']
        account.commission = data['Commission']
        account.margin = data['CurrMargin']
        account.closeProfit = data['CloseProfit']
        account.positionProfit = data['PositionProfit']
        account.tradingDay = str(data['TradingDay'])
        # 这里的balance和快期中的账户不确定是否一样，需要测试
        account.balance = (data['PreBalance'] - data['PreCredit'] - data['PreMortgage'] +
                           data['Mortgage'] - data['Withdraw'] + data['Deposit'] +
                           data['CloseProfit'] + data['PositionProfit'] + data['CashIn'] -
                           data['Commission'])
        
        # 推送
        self.gateway.onAccount(account)
    
    #----------------------------------------------------------------------
    def onRspQryInvestor(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryTradingCode(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInstrumentMarginRate(self, data, error, n, last):
        """
        获取保证金率
        :param data: 
        :param error: 
        :param n: 
        :param last: 
        :return: 
        """
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInstrumentCommissionRate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryExchange(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryProduct(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInstrument(self, data, error, n, last):
        """合约查询回报"""

        self.gateway.mdConnected = True
        contract = VtContractData()
        contract.gatewayName = self.gatewayName

        contract.symbol = data['InstrumentID']
        contract.exchange = exchangeMapReverse[data['ExchangeID']]
        contract.vtSymbol = contract.symbol #'.'.join([contract.symbol, contract.exchange])
        contract.name = data['InstrumentName']  #.decode('GBK')
        
        # 合约数值
        contract.size = data['VolumeMultiple']
        contract.priceTick = data['PriceTick']
        contract.strikePrice = data['StrikePrice']
        contract.underlyingSymbol = data['UnderlyingInstrID']
        contract.longMarginRatio = data['LongMarginRatio']
        contract.shortMarginRatio = data['ShortMarginRatio']

        contract.productClass = productClassMapReverse.get(data['ProductClass'], PRODUCT_UNKNOWN)
        
        # 期权类型
        if data['OptionsType'] == '1':
            contract.optionType = OPTION_CALL
        elif data['OptionsType'] == '2':
            contract.optionType = OPTION_PUT

        # 缓存代码和交易所的印射关系
        self.symbolExchangeDict[contract.symbol] = contract.exchange
        self.symbolSizeDict[contract.symbol] = contract.size

        # 推送
        self.gateway.onContract(contract)
        
        if last:
            self.writeLog(text.CONTRACT_DATA_RECEIVED)
    
    #----------------------------------------------------------------------
    def onRspQryDepthMarketData(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQrySettlementInfo(self, data, error, n, last):
        """查询结算信息回报"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryTransferBank(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInvestorPositionDetail(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryNotice(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQrySettlementInfoConfirm(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInvestorPositionCombineDetail(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryCFMMCTradingAccountKey(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryEWarrantOffset(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryInvestorProductGroupMargin(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryExchangeMarginRate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryExchangeMarginRateAdjust(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryExchangeRate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQrySecAgentACIDMap(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryProductExchRate(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryProductGroup(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryOptionInstrTradeCost(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryOptionInstrCommRate(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryExecOrder(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryForQuote(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryQuote(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryLock(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryLockPosition(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryInvestorLevel(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryExecFreeze(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryCombInstrumentGuard(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryCombAction(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryTransferSerial(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryAccountregister(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspError(self, error, n, last):
        """错误回报"""

        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = u'onRspError' + error['ErrorMsg']    #.decode('gbk')
        self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onRtnOrder(self, data):
        """报单回报"""
        # 更新最大报单编号
        newref = data['OrderRef']
        self.orderRef = max(self.orderRef, int(newref))
        
        # 创建报单数据对象
        order = VtOrderData()
        order.gatewayName = self.gatewayName
        
        # 保存代码和报单号
        order.symbol = data['InstrumentID']
        order.exchange = exchangeMapReverse[data['ExchangeID']]
        order.vtSymbol = order.symbol #'.'.join([order.symbol, order.exchange])
        
        order.orderID = data['OrderRef']
        # CTP的报单号一致性维护需要基于frontID, sessionID, orderID三个字段
        # 但在本接口设计中，已经考虑了CTP的OrderRef的自增性，避免重复
        # 唯一可能出现OrderRef重复的情况是多处登录并在非常接近的时间内（几乎同时发单）
        # 考虑到VtTrader的应用场景，认为以上情况不会构成问题
        order.vtOrderID = '.'.join([self.gatewayName, order.orderID])

        order.direction = directionMapReverse.get(data['Direction'], DIRECTION_UNKNOWN)
        order.offset = offsetMapReverse.get(data['CombOffsetFlag'], OFFSET_UNKNOWN)
        order.status = statusMapReverse.get(data['OrderStatus'], STATUS_UNKNOWN)
            
        # 价格、报单量等数值
        order.price = data['LimitPrice']
        order.totalVolume = data['VolumeTotalOriginal']
        order.tradedVolume = data['VolumeTraded']
        order.orderTime = data['InsertTime']
        order.updateTime = data['UpdateTime']
        order.cancelTime = data['CancelTime']
        order.frontID = data['FrontID']
        order.sessionID = data['SessionID']

        # 推送
        self.gateway.onOrder(order)
    
    #----------------------------------------------------------------------
    def onRtnTrade(self, data):
        """成交回报"""
        # 创建报单数据对象
        trade = VtTradeData()
        trade.gatewayName = self.gatewayName
        
        # 保存代码和报单号
        trade.symbol = data['InstrumentID']
        trade.exchange = exchangeMapReverse[data['ExchangeID']]
        trade.vtSymbol = trade.symbol #'.'.join([trade.symbol, trade.exchange])
        
        trade.tradeID = data['TradeID']
        trade.vtTradeID = '.'.join([self.gatewayName, trade.tradeID])
        
        trade.orderID = data['OrderRef']
        trade.vtOrderID = '.'.join([self.gatewayName, trade.orderID])
        
        # 方向
        trade.direction = directionMapReverse.get(data['Direction'], '')
            
        # 开平
        trade.offset = offsetMapReverse.get(data['OffsetFlag'], '')
            
        # 价格、报单量等数值
        trade.price = data['Price']
        trade.volume = data['Volume']
        trade.tradeTime = data['TradeTime']
        
        # 推送
        self.gateway.onTrade(trade)
    
    #----------------------------------------------------------------------
    def onErrRtnOrderInsert(self, data, error):
        """发单错误回报（交易所）"""
        # 推送委托信息
        order = VtOrderData()
        order.gatewayName = self.gatewayName
        order.symbol = data['InstrumentID']
        order.exchange = exchangeMapReverse[data['ExchangeID']]
        order.vtSymbol = order.symbol
        order.orderID = data['OrderRef']
        order.vtOrderID = '.'.join([self.gatewayName, order.orderID])
        order.direction = directionMapReverse.get(data['Direction'], DIRECTION_UNKNOWN)
        order.offset = offsetMapReverse.get(data['CombOffsetFlag'], OFFSET_UNKNOWN)
        order.status = STATUS_REJECTED
        order.price = data['LimitPrice']
        order.totalVolume = data['VolumeTotalOriginal']
        self.gateway.onOrder(order)

        # 推送错误信息
        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = error['ErrorMsg'] #.decode('gbk')
        err.additionalInfo = u'onErrRtnOrderInsert.{0},v:{1},ref:{2}:'\
            .format(order.vtSymbol , order.totalVolume, order.orderID)
        self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onErrRtnOrderAction(self, data, error):
        """撤单错误回报（交易所）"""

        symbol = data['InstrumentID']

        err = VtErrorData()
        err.gatewayName = self.gatewayName
        err.errorID = error['ErrorID']
        err.errorMsg = error['ErrorMsg']    #.decode('gbk')
        err.additionalInfo =u'onErrRtnOrderAction.{0}'.format(symbol)
        self.gateway.onError(err)
    
    #----------------------------------------------------------------------
    def onRtnInstrumentStatus(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnTradingNotice(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnErrorConditionalOrder(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnExecOrder(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnExecOrderInsert(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnExecOrderAction(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnForQuoteInsert(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnQuote(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnQuoteInsert(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnQuoteAction(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnForQuoteRsp(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnCFMMCTradingAccountToken(self, data):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRtnLock(self, data):
        """"""
        pass

    #----------------------------------------------------------------------
    def onErrRtnLockInsert(self, data, error):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRtnCombAction(self, data):
        """"""
        pass

    #----------------------------------------------------------------------
    def onErrRtnCombActionInsert(self, data, error):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRspQryContractBank(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryParkedOrder(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryParkedOrderAction(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryTradingNotice(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryBrokerTradingParams(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQryBrokerTradingAlgos(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQueryCFMMCTradingAccountToken(self, data, error, n, last):
        """"""
        pass

    #----------------------------------------------------------------------
    def onRtnFromBankToFutureByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnFromFutureToBankByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromBankToFutureByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromFutureToBankByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnFromBankToFutureByFuture(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnFromFutureToBankByFuture(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromBankToFutureByFutureManual(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromFutureToBankByFutureManual(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnQueryBankBalanceByFuture(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnBankToFutureByFuture(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnFutureToBankByFuture(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnRepealBankToFutureByFutureManual(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnRepealFutureToBankByFutureManual(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onErrRtnQueryBankBalanceByFuture(self, data, error):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromBankToFutureByFuture(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnRepealFromFutureToBankByFuture(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspFromBankToFutureByFuture(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspFromFutureToBankByFuture(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRspQueryBankAccountMoneyByFuture(self, data, error, n, last):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnOpenAccountByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnCancelAccountByBank(self, data):
        """"""
        pass
    
    #----------------------------------------------------------------------
    def onRtnChangeAccountByBank(self, data):
        """"""
        pass
    
    # ----------------------------------------------------------------------
    def connect(self, userID, password, brokerID, address, authCode, userProductInfo):
        """初始化连接"""
        self.userID = userID                # 账号
        self.password = password            # 密码
        self.brokerID = brokerID            # 经纪商代码
        self.address = address              # 服务器地址
        self.authCode = authCode            #验证码
        self.userProductInfo = userProductInfo  #产品信息

        # 如果尚未建立服务器连接，则进行连接
        if not self.connectionStatus:
            # 创建C++环境中的API对象，这里传入的参数是需要用来保存.con文件的文件夹路径
            path = os.getcwd() + '/temp/' + str(self.gatewayName) + '/'
            if not os.path.exists(path):
                os.makedirs(path)
            self.createFtdcTraderApi(path)
            
            # 设置数据同步模式为推送从今日开始所有数据
            self.subscribePrivateTopic(0)
            self.subscribePublicTopic(0)

            # 注册服务器地址
            self.registerFront(self.address)
            
            # 初始化连接，成功会调用onFrontConnected
            self.init()
            
        # 若已经连接但尚未登录，则进行登录
        else:
            if self.requireAuthentication and not self.authStatus:
                self.authenticate()
            elif not self.loginStatus:
                self.login()    
    
    # ----------------------------------------------------------------------
    def login(self):
        """连接服务器"""
        # 如果填入了用户名密码等，则登录
        if self.userID and self.password and self.brokerID:
            req = {}
            req['UserID'] = self.userID
            req['Password'] = self.password
            req['BrokerID'] = self.brokerID
            self.reqID += 1
            self.reqUserLogin(req, self.reqID)   
        
    # ----------------------------------------------------------------------
    def authenticate(self):
        """申请验证"""
        if self.userID and self.brokerID and self.authCode and self.userProductInfo:
            req = {}
            req['UserID'] = self.userID
            req['BrokerID'] = self.brokerID
            req['AuthCode'] = self.authCode
            req['UserProductInfo'] = self.userProductInfo
            self.reqID +=1
            self.reqAuthenticate(req, self.reqID)

    # ----------------------------------------------------------------------
    def qryAccount(self):
        """查询账户"""
        self.reqID += 1
        self.reqQryTradingAccount({}, self.reqID)
        
    # ----------------------------------------------------------------------
    def qryPosition(self):
        """查询持仓"""
        self.reqID += 1
        req = {}
        req['BrokerID'] = self.brokerID
        req['InvestorID'] = self.userID
        self.reqQryInvestorPosition(req, self.reqID)
        
    # ----------------------------------------------------------------------
    def sendOrder(self, orderReq):
        """发单"""
        self.reqID += 1
        self.orderRef += 1
        
        req = {}
        
        req['InstrumentID'] = orderReq.symbol
        req['LimitPrice'] = orderReq.price

        # 增加检查，ctp不支持float的volume下单
        if isinstance(orderReq.volume, float):
            req['VolumeTotalOriginal'] = int(orderReq.volume)
        else:
            req['VolumeTotalOriginal'] = orderReq.volume
        
        # 下面如果由于传入的类型本接口不支持，则会 返回空字符串
        req['OrderPriceType'] = priceTypeMap.get(orderReq.priceType, '')
        req['Direction'] = directionMap.get(orderReq.direction, '')
        req['CombOffsetFlag'] = offsetMap.get(orderReq.offset, '')
            
        req['OrderRef'] = str(self.orderRef)
        req['InvestorID'] = self.userID
        req['UserID'] = self.userID
        req['BrokerID'] = self.brokerID
        
        req['CombHedgeFlag'] = defineDict['THOST_FTDC_HF_Speculation']       # 投机单
        req['ContingentCondition'] = defineDict['THOST_FTDC_CC_Immediately'] # 立即发单
        req['ForceCloseReason'] = defineDict['THOST_FTDC_FCC_NotForceClose'] # 非强平
        req['IsAutoSuspend'] = 0                                             # 非自动挂起
        req['TimeCondition'] = defineDict['THOST_FTDC_TC_GFD']               # 今日有效
        req['VolumeCondition'] = defineDict['THOST_FTDC_VC_AV']              # 任意成交量
        req['MinVolume'] = 1                                                 # 最小成交量为1
        
        # 判断FAK和FOK
        if orderReq.priceType == PRICETYPE_FAK:
            req['OrderPriceType'] = defineDict["THOST_FTDC_OPT_LimitPrice"]
            req['TimeCondition'] = defineDict['THOST_FTDC_TC_IOC']
            req['VolumeCondition'] = defineDict['THOST_FTDC_VC_AV']
        if orderReq.priceType == PRICETYPE_FOK:
            req['OrderPriceType'] = defineDict["THOST_FTDC_OPT_LimitPrice"]
            req['TimeCondition'] = defineDict['THOST_FTDC_TC_IOC']
            req['VolumeCondition'] = defineDict['THOST_FTDC_VC_CV']        
        
        self.reqOrderInsert(req, self.reqID)
        
        # 返回订单号（字符串），便于某些算法进行动态管理
        vtOrderID = '.'.join([self.gatewayName, str(self.orderRef)])
        return vtOrderID
    
    # ----------------------------------------------------------------------
    def cancelOrder(self, cancelOrderReq):
        """撤单"""
        self.reqID += 1

        req = {}
        
        req['InstrumentID'] = cancelOrderReq.symbol
        req['ExchangeID'] = cancelOrderReq.exchange
        req['OrderRef'] = cancelOrderReq.orderID
        req['FrontID'] = cancelOrderReq.frontID
        req['SessionID'] = cancelOrderReq.sessionID
        
        req['ActionFlag'] = defineDict['THOST_FTDC_AF_Delete']
        req['BrokerID'] = self.brokerID
        req['InvestorID'] = self.userID
        
        self.reqOrderAction(req, self.reqID)
        
    # ----------------------------------------------------------------------
    def close(self):
        """关闭"""
        self.exit()

    # ----------------------------------------------------------------------
    def writeLog(self, content):
        """发出日志"""
        log = VtLogData()
        log.gatewayName = self.gatewayName
        log.logContent = content
        self.gateway.onLog(log)

print(u'fininsh load ctpGateway.py')

#----------------------------------------------------------------------
def test():
    """测试"""
    from qtpy import QtCore
    import sys
    
    def print_log(event):
        log = event.dict_['data']
        print(':'.join([log.logTime, log.logContent]))
    
    app = QtCore.QCoreApplication(sys.argv)    

    eventEngine = EventEngine()
    eventEngine.register(EVENT_LOG, print_log)
    eventEngine.start()
    
    gateway = CtpGateway(eventEngine)
    gateway.connect()
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    test()