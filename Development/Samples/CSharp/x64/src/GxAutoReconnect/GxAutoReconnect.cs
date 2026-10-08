using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using GxIAPINET;

namespace GxAutoConnect
{
    class Sample
    {

        /// <summary>
        /// 重连回调函数
        /// </summary>
        /// <param name="pUserParam">用户私有参数</param>
        private static void __OnDeviceReconnectCallbackFun(object pUserParam)
        {
            Console.Write("The reconnect callback is triggered!\n");
        }

        /// <summary>
        /// 断线回调函数
        /// </summary>
        /// <param name="pUserParam">用户私有参数</param>
        private static void __OnDeviceDisconnectCallbackFun(object pUserParam)
        {
            Console.Write("The disconnect callback is triggered!\n");
        }

        static public IGXFeatureControl m_remoteFeatureControl = null;

        static void Main(string[] args)
        {
            try
            {
                // initialization
                IGXFactory.GetInstance().Init();

                // Enumerating cameras
                List<IGXDeviceInfo> listGxDeviceInfo = new List<IGXDeviceInfo>();
                IGXFactory.GetInstance().UpdateAllDeviceList(300, listGxDeviceInfo);
                if (listGxDeviceInfo.Count < 1)
                {
                    Console.WriteLine("Device not found");
                    IGXFactory.GetInstance().Uninit();
                    Console.WriteLine("Press any key to exit...");
                    Console.ReadKey();
                    return;
                }

                Int32 nDeviceCount = listGxDeviceInfo.Count;
                Console.WriteLine("Found Device: " + nDeviceCount);

                Int32 index = 0;
                foreach (IGXDeviceInfo deviceInfo in listGxDeviceInfo)
                {
                    Console.WriteLine($"\tID: {index}, SN: {deviceInfo.GetSN()}");
                    Console.WriteLine();
                    ++index;
                }

                index = 0;
                string deviceSN = listGxDeviceInfo[index].GetSN();
                IGXDevice objDevice = IGXFactory.GetInstance().OpenDeviceBySN(deviceSN, GX_ACCESS_MODE.GX_ACCESS_CONTROL);
                m_remoteFeatureControl = objDevice.GetRemoteFeatureControl();

                // Restore default parameter group
                m_remoteFeatureControl.GetEnumFeature("UserSetSelector").SetValue("Default");
                m_remoteFeatureControl.GetCommandFeature("UserSetLoad").Execute();

                // Open the specified flow channel
                IGXStream objStream = objDevice.OpenStream(0);

                bool bImplemented = objDevice.GetFeatureControl().IsImplemented("EnableAutoConnection");
                if(!bImplemented)
                {
					Console.WriteLine("该相机不支持断线重连功能!");
                    objStream.Close();
                    objDevice.Close();
                    IGXFactory.GetInstance().Uninit();
                    return;
                }

                objDevice.GetFeatureControl().GetBoolFeature("EnableAutoConnection").SetValue(true);
                DeviceReconnectDelegate reconnectDelegate =
                new DeviceReconnectDelegate(__OnDeviceReconnectCallbackFun);
                DeviceDisconnectDelegate disconnectDelegate =
                new DeviceDisconnectDelegate(__OnDeviceDisconnectCallbackFun);
                
                // Register reconnect callback
                objDevice.RegisterDeviceReconnectCallback(null, reconnectDelegate);
                // Register disconnect callback
                objDevice.RegisterDeviceDisconnectCallback(null, disconnectDelegate);

                Console.WriteLine("请手动插拔相机触发掉线，测试完成后点击回车完成测试!\n");
                Console.ReadKey();

                // Unregister reconnect callback
                objDevice.UnregisterDeviceReconnectCallback();
                // Unregister disconnect callback
                objDevice.UnregisterDeviceDisconnectCallback();

                objStream.Close();
                objDevice.Close();
                IGXFactory.GetInstance().Uninit();
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("GalaxyException: " + ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("Exception: " + ex.Message);
            }

            Console.WriteLine("Press any key to exit...");
            Console.ReadKey();
        }

    }
}