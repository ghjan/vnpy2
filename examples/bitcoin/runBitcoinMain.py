# encoding: UTF-8


# 重载sys模块，设置默认字符串编码方式为utf8
import sys
#reload(sys)
#sys.setdefaultencoding('utf8')

import sys
import os
import ctypes
import platform
system = platform.system()

# 将repostory的目录，作为根目录，添加到系统环境中。
ROOT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..' , '..'))
sys.path.append(ROOT_PATH)

from vnpy.trader.vtEngine import MainEngine
from vnpy.trader.uiQt import createQApp
from vnpy.trader.uiMainWindow import *

# 加载底层接口模块
from vnpy.trader.gateway import okexGateway
from vnpy.trader.gateway import binanceGateway
# 初始化的接口模块，以及其指定的名称,CTP是模块，value，是该模块下的多个连接配置文件,如 CTP_JR_connect.json    'CTP_Prod', 'CTP_JR', , 'CTP_JK', 'CTP_02'
init_gateway_names = {'OKEX': ['OKEX_1'],'BINANCE':['BINANCE_2']}
gateway_model_dict = {'OKEX': okexGateway, 'BINANCE': binanceGateway}

from vnpy.trader.app import (ctaStrategy, riskManager, spreadTrading)

# 文件路径名
path = os.path.abspath(os.path.dirname(__file__))
ICON_FILENAME = 'vnpy.ico'
ICON_FILENAME = os.path.join(path, ICON_FILENAME)

from vnpy.trader.setup_logger import setup_logger

# ----------------------------------------------------------------------
def main():
    """主程序入口"""

    logger = setup_logger(filename='logs/vnpy', debug=True)

    # 创建Qt应用对象
    qApp = createQApp()

    # 创建事件引擎
    ee = EventEngine2()

    # 初始化主引擎和主窗口对象
    mainEngine = MainEngine(ee)

    mainEngine.logger = logger

    # 添加Gatway
    for gw_name in init_gateway_names.keys():
        for gw_conf in init_gateway_names[gw_name]:
            print('add {}'.format(gw_conf))
            mainEngine.addGateway(gateway_model_dict[gw_name], gw_conf)

    # 添加应用
    mainEngine.addApp(ctaStrategy)
    mainEngine.addApp(riskManager)
    mainEngine.addApp(spreadTrading)

    mainWindow = MainWindow(mainEngine, ee)
    mainWindow.showMaximized()
    # 在主线程中启动Qt事件循环
    sys.exit(qApp.exec_())

if __name__ == '__main__':
    try:
        main()
    except Exception as ex:
        print(str(ex))
        traceback.print_exc()
