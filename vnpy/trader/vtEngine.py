# encoding: UTF-8

print(u'启动load vtEngine.py')

import shelve
from collections import OrderedDict
import os,sys
import copy

from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure,AutoReconnect
#import vnpy.trader.mongo_proxy

from vnpy.trader.vtEvent import Event as vn_event
from vnpy.trader.language import text
#from vnpy.trader.app.ctaStrategy.ctaEngine import CtaEngine
#from vnpy.trader.app.dataRecorder.drEngine import DrEngine
#from vnpy.trader.app.riskManager.rmEngine import RmEngine
from vnpy.trader.vtFunction import loadMongoSetting, getTempPath
from vnpy.trader.vtGateway import *
from vnpy.trader.app import (ctaStrategy,cmaStrategy, riskManager)
from vnpy.trader.setup_logger import setup_logger
import traceback

import psutil
try:
    from .util_mail import *
except:
    print('import util_mail fail',file=sys.stderr)
try:
    from .util_wechat import *
except:
    print('import util_wechat fail',file=sys.stderr)

LOG_DB_NAME = 'vt_logger'

########################################################################
class MainEngine(object):
    """主引擎"""

    #----------------------------------------------------------------------
    def __init__(self, eventEngine):
        """Constructor"""
        # 记录今日日期
        self.todayDate = datetime.now().strftime('%Y%m%d')

        # 创建事件引擎
        self.eventEngine = eventEngine
        self.eventEngine.start()
        
        # 创建数据引擎
        self.dataEngine = DataEngine(self, self.eventEngine)
        
        # MongoDB数据库相关
        self.dbClient = None    # MongoDB客户端对象
        self.db_has_connected = False

        # 接口实例
        self.gatewayDict = OrderedDict()
        self.gatewayDetailList = []

        # 应用模块实例
        self.appDict = OrderedDict()
        self.appDetailList = []

        # 扩展模块
        self.ctaEngine = None       # CtaEngine(self, self.eventEngine)  # cta策略运行模块
        self.drEngine = None        # DrEngine(self, self.eventEngine)    # 数据记录模块
        self.rmEngine = None        #   RmEngine(self, self.eventEngine)    # 风险管理模块
        self.cmaEngine = None       # 跨市场套利引擎
        self.connected_gw_names = []

        self.save_contract_counter = 0

        self.logger = None

        self.createLogger()

    # ----------------------------------------------------------------------
    def addGateway(self, gatewayModule,gateway_name=EMPTY_STRING):
        """添加底层接口"""
        # 是否使用指定的gateway_name
        if gateway_name==EMPTY_STRING:
            gatewayName = gatewayModule.gatewayName
        else:
            gatewayName = gateway_name

        self.writeLog(u'add gateway:{}'.format(gateway_name))

        # 创建接口实例
        self.gatewayDict[gatewayName] = gatewayModule.gatewayClass(self.eventEngine,
                                                                   gatewayName)

        # 设置接口轮询
        if gatewayModule.gatewayQryEnabled:
            self.gatewayDict[gatewayName].setQryEnabled(gatewayModule.gatewayQryEnabled)

        # 保存接口详细信息
        d = {
            'gatewayName': gatewayName,                         #gatewayModule.gatewayName,
            'gatewayDisplayName': gatewayName,                  #gatewayModule.gatewayDisplayName,
            'gatewayType': gatewayModule.gatewayType
        }
        self.gatewayDetailList.append(d)

    # ----------------------------------------------------------------------
    def addApp(self, appModule):
        """添加上层应用"""
        appName = appModule.appName

        # 创建应用实例
        self.appDict[appName] = appModule.appEngine(self, self.eventEngine)

        # 将应用引擎实例添加到主引擎的属性中
        self.__dict__[appName] = self.appDict[appName]

        # 兼容旧的self.ctaEngine/self.rmEngine
        if appName == ctaStrategy.appName:
            self.ctaEngine = self.appDict[appName]
        elif appName == riskManager.appName:
            self.rmEngine = self.appDict[appName]
        elif appName == cmaStrategy.appName:
            self.cmaEngine = self.appDict[appName]

        # 保存应用信息
        d = {
            'appName': appModule.appName,
            'appDisplayName': appModule.appDisplayName,
            'appWidget': appModule.appWidget,
            'appIco': appModule.appIco
        }
        self.appDetailList.append(d)

    # ----------------------------------------------------------------------
    def connect(self, gatewayName):
        """连接特定名称的接口"""
        if gatewayName in self.gatewayDict:
            self.writeLog(u'vtEngine conncet :{}'.format(gatewayName))
            gateway = self.gatewayDict[gatewayName]
            gateway.connect()

            if gatewayName not in self.connected_gw_names:
                self.connected_gw_names.append(gatewayName)

            # 接口连接后自动执行数据库连接的任务
            self.dbConnect()
            return True
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))
            return False

    def checkGatewayStatus(self,gatewayName):
        """check gateway connect status"""
        # 借用检查网关状态来持久化合约数据
        self.save_contract_counter += 1
        if self.save_contract_counter > 60 and self.dataEngine is not None:
            self.writeLog(u'保存持久化合约数据')
            self.dataEngine.saveContracts()
            self.save_contract_counter = 0

        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]

            return gateway.checkStatus()
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))
            return False

    def qryStatus(self):
        """查询Status"""
        status_dict = OrderedDict()

        # gateway Status
        gw_status_dict = OrderedDict()
        for k,g in self.gatewayDict.items():
            gw_status_dict[k] = g.checkStatus()
        status_dict['gateways']=gw_status_dict

        # ctaEngine Status
        if self.ctaEngine:
            tick_dict,strategy_dict = self.ctaEngine.qryStatus()
            status_dict['ticks']=tick_dict
            status_dict['strategies'] = strategy_dict

        # cpu/mem status
        cpuPercent = psutil.cpu_percent()
        memoryPercent = psutil.virtual_memory().percent
        server_info_dict = {'cpu':cpuPercent, 'mem':memoryPercent}
        status_dict['server'] = server_info_dict
#
        event = vn_event(type_=EVENT_STATUS)
        event.dict_['data']= status_dict
        self.eventEngine.put(event)

        return True

    # ----------------------------------------------------------------------
    def subscribe(self, subscribeReq, gatewayName):
        """订阅特定接口的行情"""
        # 处理没有输入gatewayName的情况
        if gatewayName is None or len(gatewayName) == 0:
            if len(self.connected_gw_names) == 0:
                self.writeError(u'vtEngine.subscribe, no connected gateway')
                return
            for gw_name in self.connected_gw_names:
                gateway = self.gatewayDict[gw_name]
                gateway.subscribe(subscribeReq)
            return

        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            gateway.subscribe(subscribeReq)
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))
        
    # ----------------------------------------------------------------------
    def sendOrder(self, orderReq, gatewayName):
        """对特定接口发单"""
        # 如果风控检查失败则不发单
        if self.rmEngine and not self.rmEngine.checkRisk(orderReq):
            self.writeCritical(u'风控检查不通过,gw:{},{} {} {} p:{} v:{}'.format(gatewayName, orderReq.direction, orderReq.offset, orderReq.symbol, orderReq.price, orderReq.volume))
            return ''

        if self.rmEngine and self.rmEngine.active\
                and self.dataEngine and \
                self.dataEngine.check_self_trade_risk(vtSymbol=orderReq.symbol,direction=orderReq.direction, price=orderReq.price, gatewayName=gatewayName):
            self.writeCritical(
                u'自成交检查不通过,gw:{},{} {} {} p:{} v:{}'.format(gatewayName, orderReq.direction, orderReq.offset,
                                                           orderReq.symbol, orderReq.price, orderReq.volume))
            return ''

        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            return gateway.sendOrder(orderReq)
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))
    
    # ----------------------------------------------------------------------
    def cancelOrder(self, cancelOrderReq, gatewayName):
        """对特定接口撤单"""
        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            gateway.cancelOrder(cancelOrderReq)
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))

    # ----------------------------------------------------------------------
    def qryAccount(self, gatewayName):
        """查询特定接口的账户"""
        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            gateway.qryAccount()
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))

    def getAccountInfo(self,gatewayName=None):
        """读取风控的账号与仓位数据
        # Added by IncenseLee
        仅支持一个账号。不支持多账号
        以后支持跨市场套利才更新吧。
        return 当前账号的权益、可用资金、当前仓位比例, 投资仓位比例上限
        """
        if gatewayName is None and len(self.connected_gw_names) > 1:
            raise ValueError(u'多个网关连接，须指明gateway_name参数')
        if self.rmEngine:
            return self.rmEngine.getAccountInfo()
        else:
            return 0, 0, 0, 0

    def qryAccountNo(self,gatewayName):
        """
         根据gateway名称，返回账号
        :param gatewayName: 
        :return: 
        """
        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            if gateway.accountID:
                return copy.copy(gateway.accountID)

        return gatewayName
    # ----------------------------------------------------------------------
    def qryPosition(self, gatewayName):
        """查询特定接口的持仓"""
        if gatewayName in self.gatewayDict:
            gateway = self.gatewayDict[gatewayName]
            gateway.qryPosition()
        else:
            self.writeLog(text.GATEWAY_NOT_EXIST.format(gateway=gatewayName))
        
    # ----------------------------------------------------------------------
    def exit(self):
        """退出程序前调用，保证正常退出"""        
        # 安全关闭所有接口
        for gateway in list(self.gatewayDict.values()):
            gateway.close()
        
        # 停止事件引擎
        self.eventEngine.stop()      
        
        # 停止数据记录引擎
        if self.drEngine:
            self.drEngine.stop()

        # 保存数据引擎里的合约数据到硬盘
        self.dataEngine.saveContracts()

    def disconnect(self, gateway_name=EMPTY_STRING):
        """断开底层gateway的连接"""
        try:
            # 只断开指定的gateway
            if gateway_name != EMPTY_STRING:
                if gateway_name in self.gatewayDict:
                    self.writeLog(u'获取{} gateway'.format(gateway_name))
                    gateway = self.gatewayDict[gateway_name]
                    gateway.close()
                    if gateway_name in self.connected_gw_names:
                        self.writeLog(u'移除connected_gw_names[{}]'.format(gateway_name))
                        self.connected_gw_names.remove(gateway_name)
                    return
                else:
                    self.writeLog(u'gateway接口不存在：%s' % gateway_name)

            # 断开所有的gateway
            for gateway in list(self.gatewayDict.values()):
                gateway.close()

            return True

        except Exception as ex:
            print( u'vtEngine.disconnect Exception:{0} '.format(str(ex)))
            return False

    # ----------------------------------------------------------------------
    def writeLog(self, content):
        """快速发出日志事件"""
        log = VtLogData()
        log.logContent = content
        event = vn_event(type_ = EVENT_LOG)
        event.dict_['data'] = log
        self.eventEngine.put(event)

        # 写入本地log日志
        if self.logger is not None:
            self.logger.info(content)
        else:
            self.createLogger()

    def createLogger(self):
        """
        创建日志记录
        :return: 
        """
        currentFolder = os.path.abspath(os.path.join(os.getcwd(), 'logs'))
        if os.path.isdir(currentFolder):
            # 如果工作目录下，存在data子目录，就使用data子目录
            path = currentFolder
        else:
            # 否则，使用缺省保存目录 vnpy/trader/app/ctaStrategy/data
            path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'logs'))

        filename = os.path.abspath(os.path.join(path, 'vnpy'))

        print( u'create logger:{}'.format(filename))
        self.logger = setup_logger(filename=filename, name='vnpy', debug=True)

    # ----------------------------------------------------------------------
    def writeError(self, content):
        """快速发出错误日志事件"""
        log = VtErrorData()
        log.errorMsg = content
        event = vn_event(type_=EVENT_ERROR)
        event.dict_['data'] = log
        self.eventEngine.put(event)

        # 写入本地log日志
        if self.logger is not None:
            self.logger.error(content)
            print('{}'.format(datetime.now()),file=sys.stderr)
            print(content, file=sys.stderr)
        else:
            print(content, file=sys.stderr)
            self.createLogger()

        # 发出邮件/微信
       #try:
       #    if len(self.gatewayDetailList) > 0:
       #        target = self.gatewayDetailList[0]['gatewayName']
       #    else:
       #        target = WECHAT_GROUP["DEBUG_01"]
       #    sendWeChatMsg(content, target=target, level=WECHAT_LEVEL_ERROR)
       #except Exception as ex:
       #    print(u'send wechat exception:{}'.format(str(ex)),file=sys.stderr)

    # ----------------------------------------------------------------------
    def writeWarning(self, content):
        """快速发出告警日志事件"""
        log = VtLogData()
        log.logContent = content
        event = vn_event(type_=EVENT_WARNING)
        event.dict_['data'] = log
        self.eventEngine.put(event)

        # 写入本地log日志
        if self.logger is not None:
            self.logger.warning(content)
        else:
            print(content,file=sys.stderr)
            self.createLogger()

        # 发出邮件
        try:
            sendmail(subject=u'{0} Warning'.format('_'.join(self.connected_gw_names)), msgcontent=content)
        except:
            pass

        # 发出微信
        #try:
        #    if len(self.gatewayDetailList) > 0:
        #        target = self.gatewayDetailList[0]['gatewayName']
        #    else:
        #        target = WECHAT_GROUP["DEBUG_01"]
        #    sendWeChatMsg(content, target=target, level=WECHAT_LEVEL_WARNING)
        #except Exception as ex:
        #    print(u'send wechat exception:{}'.format(str(ex)), file=sys.stderr)

    # ----------------------------------------------------------------------
    def writeNotification(self, content):
        """快速发出通知日志事件"""
        log = VtLogData()
        log.logContent = content
        event = vn_event(type_=EVENT_NOTIFICATION)
        event.dict_['data'] = log
        self.eventEngine.put(event)

        # 发出邮件
        try:
            sendmail(subject=u'{0} Notification'.format('_'.join(self.connected_gw_names)), msgcontent=content)
        except:
            pass

        # 发出微信
       # try:
       #     if len(self.gatewayDetailList) > 0:
       #         target = self.gatewayDetailList[0]['gatewayName']
       #     else:
       #         target = WECHAT_GROUP["DEBUG_01"]
       #     sendWeChatMsg(content, target=target, level=WECHAT_LEVEL_INFO)
       # except Exception as ex:
       #     print(u'send wechat exception:{}'.format(str(ex)), file=sys.stderr)

    # ----------------------------------------------------------------------
    def writeCritical(self, content):
        """快速发出严重错误日志事件"""

        log = VtLogData()
        log.logContent = content
        event = vn_event(type_=EVENT_CRITICAL)
        event.dict_['data'] = log
        self.eventEngine.put(event)

        # 写入本地log日志
        if self.logger:
            self.logger.critical(content)
            print('{}'.format(datetime.now()), file=sys.stderr)
            print(content, file=sys.stderr)
        else:
            print(content, file=sys.stderr)
            self.createLogger()

        # 发出邮件
        try:
            sendmail(subject=u'{0} Critical'.format('_'.join(self.connected_gw_names)), msgcontent=content)
        except:
            pass

        ## 发出微信
        #try:
        # #   if len(self.gatewayDetailList) > 0:
        #        target = self.gatewayDetailList[0]['gatewayName']
        #    else:
        #        target = WECHAT_GROUP["DEBUG_01"]
        #    sendWeChatMsg(content, target=target, level=WECHAT_LEVEL_FATAL)
        #except:
        #    pass
#
    # ----------------------------------------------------------------------
    def dbConnect(self):
        """连接MongoDB数据库"""
        if not self.dbClient:
            # 读取MongoDB的设置
            host, port, logging = loadMongoSetting()
                
            try:
                # 设置MongoDB操作的超时时间为0.5秒
                self.dbClient = MongoClient(host, port, connectTimeoutMS=500)
                
                # 调用server_info查询服务器状态，防止服务器异常并未连接成功
                self.dbClient.server_info()

                self.writeLog(text.DATABASE_CONNECTING_COMPLETED)
                self.db_has_connected = True

                # 如果启动日志记录，则注册日志事件监听函数
                #if logging:
                #    self.eventEngine.register(EVENT_LOG, self.dbLogging)

            except ConnectionFailure:
                self.dbClient = None
                self.writeError(text.DATABASE_CONNECTING_FAILED)
                self.db_has_connected = False

    
    # ----------------------------------------------------------------------
    def dbInsert(self, dbName, collectionName, d):
        """向MongoDB中插入数据，d是具体数据"""
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]
                collection.insert_one(d)
            else:
                self.writeLog(text.DATA_INSERT_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()

        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except Exception as ex:
            self.writeError(u'dbInsert exception:{}'.format(str(ex)))

    def dbInsertMany(self,dbName, collectionName, data_list,ordered=True):
        """
        向MongoDB中插入数据，data_list是具体数据 列表
        :param dbName:
        :param collectionName:
        :param data_list:
        :param ordered: 是否忽略insert error
        :return:
        """
        if not isinstance(data_list,list):
            self.writeLog(text.DATA_INSERT_FAILED)
            return
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]
                collection.insert_many(data_list, ordered = ordered)
            else:
                self.writeLog(text.DATA_INSERT_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()

        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except Exception as ex:
            self.writeError(u'dbInsertMany exception:{}'.format(str(ex)))

    # ----------------------------------------------------------------------
    def dbQuery(self, dbName, collectionName, d, sortKey='', sortDirection=ASCENDING):
        """从MongoDB中读取数据，d是查询要求，返回的是数据库查询的指针"""
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]

                if sortKey:
                    cursor = collection.find(d).sort(sortKey, sortDirection)  # 对查询出来的数据进行排序
                else:
                    cursor = collection.find(d)

                if cursor:
                    return list(cursor)
                else:
                        return []
            else:
                self.writeLog(text.DATA_QUERY_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()

        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except Exception as ex:
            self.writeError(u'dbQuery exception:{}'.format(str(ex)))

        return []

    def dbQueryBySort(self, dbName, collectionName, d, sortName, sortType, limitNum=0):
        """从MongoDB中读取数据，d是查询要求，sortName是排序的字段,sortType是排序类型
          返回的是数据库查询的指针"""
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]
                if limitNum > 0:
                    cursor = collection.find(d).sort(sortName, sortType).limit(limitNum)
                else:
                    cursor = collection.find(d).sort(sortName, sortType)
                if cursor:
                    return list(cursor)
                else:
                    return []
            else:
                self.writeLog(text.DATA_QUERY_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()

        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except Exception as ex:
            self.writeError(u'dbQueryBySort exception:{}'.format(str(ex)))

        return []
    #----------------------------------------------------------------------
    def dbUpdate(self, dbName, collectionName, d, flt, upsert=False):
        """向MongoDB中更新数据，d是具体数据，flt是过滤条件，upsert代表若无是否要插入"""
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]
                collection.replace_one(flt, d, upsert)
            else:
                self.writeLog(text.DATA_UPDATE_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()

        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except Exception as ex:
            self.writeError(u'dbUpdate exception:{}'.format(str(ex)))

    def dbDelete(self,dbName, collectionName, flt):
        """
        向mongodb中，删除数据，flt是过滤条件
        :param dbName:
        :param collectionName:
        :param flt:
        :return:
        """
        try:
            if self.dbClient:
                db = self.dbClient[dbName]
                collection = db[collectionName]
                collection.delete_many(flt)
            else:
                self.writeLog(text.DATA_DELETE_FAILED)
                if self.db_has_connected:
                    self.writeLog(u'重新尝试连接数据库')
                    self.dbConnect()
        except ConnectionFailure:
            self.dbClient = None
            self.writeError(u'数据库连接断开')
            if self.db_has_connected:
                self.writeLog(u'重新尝试连接数据库')
                self.dbConnect()
        except AutoReconnect as ex:
            self.writeError(u'数据库连接断开重连:{}'.format(str(ex)))
            time.sleep(1)
        except Exception as ex:
            self.writeError(u'dbDelete exception:{}'.format(str(ex)))

    #----------------------------------------------------------------------
    def dbLogging(self, event):
        """向MongoDB中插入日志"""
        log = event.dict_['data']
        d = {
            'content': log.logContent,
            'time': log.logTime,
            'gateway': log.gatewayName
        }
        self.dbInsert(LOG_DB_NAME, self.todayDate, d)
    
    #----------------------------------------------------------------------
    def getContract(self, vtSymbol):
        """查询合约"""
        return self.dataEngine.getContract(vtSymbol)

    #----------------------------------------------------------------------
    def getAllContracts(self):
        """查询所有合约（返回列表）"""
        return self.dataEngine.getAllContracts()
    
    #----------------------------------------------------------------------
    def getOrder(self, vtOrderID):
        """查询委托"""
        return self.dataEngine.getOrder(vtOrderID)
    
    #----------------------------------------------------------------------
    def getAllWorkingOrders(self):
        """查询所有的活跃的委托（返回列表）"""
        return self.dataEngine.getAllWorkingOrders()

    # ----------------------------------------------------------------------
    def getAllGatewayNames(self):
        """查询引擎中所有可用接口的名称"""
        return self.gatewayDict.keys()
        # ----------------------------------------------------------------------

    def getAllGatewayDetails(self):
        """查询引擎中所有底层接口的信息"""
        return self.gatewayDetailList

        # ----------------------------------------------------------------------

    def getAllAppDetails(self):
        """查询引擎中所有上层应用的信息"""
        return self.appDetailList

    # ----------------------------------------------------------------------
    def getApp(self, appName):
        """获取APP引擎对象"""
        return self.appDict[appName]

    def clearData(self):
        """清空数据引擎的数据"""
        if self.dataEngine:
            self.dataEngine.clearData()

        if self.ctaEngine:
            self.ctaEngine.clearData()

    def saveData(self):
        if self.ctaEngine:
            self.ctaEngine.saveStrategyData()

    def initStrategy(self,name, force = False):
        if not self.ctaEngine:
            self.writeError(u'Cta Engine not started')
            return
        self.ctaEngine.initStrategy(name=name, force=force)
        self.qryStatus()

    def startStrategy(self,name):
        if not self.ctaEngine:
            self.writeError(u'Cta Engine not started')
            return
        self.ctaEngine.startStrategy(name=name)
        self.qryStatus()

    def stopStrategy(self,name):
        if not self.ctaEngine:
            self.writeError(u'Cta Engine not started')
            return
        self.ctaEngine.stopStrategy(name=name)
        self.qryStatus()

########################################################################
class DataEngine(object):
    """数据引擎"""
    contractFileName = 'ContractData.vt'
    contractFilePath = getTempPath(contractFileName)

    # ----------------------------------------------------------------------
    def __init__(self, mainEngine, eventEngine):
        """Constructor"""
        self.mainEngine = mainEngine
        self.eventEngine = eventEngine
        
        # 保存合约详细信息的字典
        self.contractDict = {}
        
        # 保存委托数据的字典
        self.orderDict = {}
        
        # 保存活动委托数据的字典（即可撤销）
        self.workingOrderDict = {}
        
        # 读取保存在硬盘的合约数据
        self.loadContracts()
        
        # 注册事件监听
        self.registerEvent()

        # 已订阅合约代码
        self.subscribedSymbols = set()
        
    # ----------------------------------------------------------------------
    def updateContract(self, event):
        """更新合约数据"""
        contract = event.dict_['data']
        self.contractDict[contract.vtSymbol] = contract
        self.contractDict[contract.symbol] = contract       # 使用常规代码（不包括交易所）可能导致重复
        
    # ----------------------------------------------------------------------
    def getContract(self, vtSymbol):
        """查询合约对象"""
        try:
            return self.contractDict[vtSymbol]
        except KeyError:
            return None
        
    # ----------------------------------------------------------------------
    def getAllContracts(self):
        """查询所有合约对象（返回列表）"""
        return list(self.contractDict.values())
    
    # ----------------------------------------------------------------------
    def saveContracts(self):
        """保存所有合约对象到硬盘"""
        self.mainEngine.writeLog(u'持久化合约数据')
        f = shelve.open(self.contractFilePath)
        f['data'] = self.contractDict
        f.close()
    
    # ----------------------------------------------------------------------
    def loadContracts(self):
        """从硬盘读取合约对象"""
        print(u'load contract data from:{}'.format(self.contractFilePath))
        f = shelve.open(self.contractFilePath)
        if 'data' in f:
            d = f['data']
            for key, value in d.items():
                self.contractDict[key] = value
        f.close()
        
    # ----------------------------------------------------------------------
    def updateOrder(self, event):
        """更新委托数据"""
        order = event.dict_['data']        
        self.orderDict[order.vtOrderID] = order
        
        # 如果订单的状态是全部成交或者撤销，则需要从workingOrderDict中移除
        if order.status in [STATUS_ALLTRADED, STATUS_REJECTED, STATUS_CANCELLED]:
            if order.vtOrderID in self.workingOrderDict:
                del self.workingOrderDict[order.vtOrderID]
        # 否则则更新字典中的数据        
        else:
            self.workingOrderDict[order.vtOrderID] = order

    def check_self_trade_risk(self, vtSymbol, direction,  price, gatewayName):
        """
        检查自成交
        :param vtSymbol:
        :param direction:
        :param price:
        :return:True;有风险；False:无风险
        """
        if len(self.workingOrderDict) == 0:
            return False

        try:
            if direction == DIRECTION_LONG:
                for order in list(self.workingOrderDict.values()):
                    if order.vtSymbol == vtSymbol and order.direction == DIRECTION_SHORT and order.gatewayName==gatewayName and order.price <= price:
                        self.mainEngine.writeNotification(u'存在反向委托单:id:{},{},gw:{},order.price:{}<{}，有自成交风险'.
                                                          format(order.vtOrderID,order.direction,order.gatewayName, order.price,price))
                        return True
            elif direction == DIRECTION_SHORT:
                for order in list(self.workingOrderDict.values()):
                    if order.vtSymbol == vtSymbol and order.direction == DIRECTION_LONG and order.gatewayName == gatewayName and order.price >= price:
                        self.mainEngine.writeNotification(u'存在反向委托单:id:{},{},gw:{},order.price:{}>{}，有自成交风险'.
                                                          format(order.vtOrderID, order.direction, order.gatewayName,
                                                                 order.price, price))
                        return True

            return False
        except Exception as ex:
            self.mainEngine.writeCritical(u'DataEngine check_self_trade_risk Exception:{} /{}'.format(str(ex),traceback.format_exc()))
            return False

    # ----------------------------------------------------------------------
    def getOrder(self, vtOrderID):
        """查询委托单（报单）"""
        try:
            return self.orderDict[vtOrderID]
        except KeyError:
            return None
    
    # ----------------------------------------------------------------------
    def getAllWorkingOrders(self):
        """查询所有活动委托（返回列表）"""
        return list(self.workingOrderDict.values())
    
    # ----------------------------------------------------------------------
    def registerEvent(self):
        """注册事件监听"""
        self.eventEngine.register(EVENT_CONTRACT, self.updateContract)
        self.eventEngine.register(EVENT_ORDER, self.updateOrder)
        self.eventEngine.register(EVENT_POSITION, self.updatePosition)
        
    def clearData(self):
        """清空数据"""

        self.orderDict = {}
        self.workingOrderDict = {}
        self.subscribedSymbols.clear()

    def updatePosition(self,event):
        """更新持仓信息"""
        # 在获取更新持仓信息时，自动订阅这个symbol
        # 目的：1、

        position = event.dict_['data']
        symbol = position.symbol

        if symbol is None:
            return

        if len(symbol) == 0:
            return

        # 已存在，不做更新
        if symbol in self.subscribedSymbols:
            return

        self.subscribedSymbols.add(symbol)

        gatewayName = position.gatewayName
        contract = self.mainEngine.getContract(symbol)

        if not contract:
            self.mainEngine.writeLog(u'vtEngine.updatePosition()找不到合约{0}信息'.format(symbol))
            return

        # 订阅合约
        req = VtSubscribeReq()
        req.symbol = symbol
        req.exchange = contract.exchange
        req.currency = ''
        req.productClass = ''

        self.mainEngine.subscribe(req, gatewayName)

        self.mainEngine.writeLog(u'自动订阅合约{0}'.format(symbol))

