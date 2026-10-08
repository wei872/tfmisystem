using System.Runtime.InteropServices;
using GxIAPINET;

namespace GxActionCommand
{
    static class Sample
    {
        // 检查是否所有相机都支持ActionCommand和ptp功能
        static void CheckCamParameters(List<IGXDevice> lstDevPtr, List<IGXFeatureControl> lstRemoteFeature)
        {
            Console.WriteLine("check is all device support ActionCommand and PTP");

            bool bIsAllDevSupport = true;
            for (int i = 0; i < lstRemoteFeature.Count; i++)
            {
                List<string> lstEnumValue = lstRemoteFeature[i].GetEnumFeature("GevSupportedOptionSelector").GetEnumEntryList();

                bool bActionItemExist = false;
                bool bScheduledActionItemExist = false;
                bool bPtpItemExist = false;

                bool bActionSupport = false;
                bool bScheduledActionSupport = false;
                bool bPtpSupport = false;

                foreach (string EnumValue in lstEnumValue)
                {
                    if (EnumValue.Equals("Action"))
                    {
                        bActionItemExist = true;
                    }

                    if (EnumValue.Equals("ScheduledAction"))
                    {
                        bScheduledActionItemExist = true;
                    }

                    if (EnumValue.Equals("Ptp"))
                    {
                        bPtpItemExist = true;
                    }
                }

                if (bActionItemExist && bScheduledActionItemExist && bPtpItemExist)
                {
                    lstRemoteFeature[i].GetEnumFeature("GevSupportedOptionSelector").SetValue("Action");

                    bActionSupport = lstRemoteFeature[i].GetBoolFeature("GevSupportedOption").GetValue();

                    lstRemoteFeature[i].GetEnumFeature("GevSupportedOptionSelector").SetValue("ScheduledAction");

                    bScheduledActionSupport = lstRemoteFeature[i].GetBoolFeature("GevSupportedOption").GetValue();

                    lstRemoteFeature[i].GetEnumFeature("GevSupportedOptionSelector").SetValue("Ptp");

                    bPtpSupport = lstRemoteFeature[i].GetBoolFeature("GevSupportedOption").GetValue();

                    if (bActionSupport && bScheduledActionSupport && bPtpSupport)
                    {
                        // 当前相机支持 Action ScheduledAction Ptp
                    }
                    else
                    {
                        string SN = lstDevPtr[i].GetDeviceInfo().GetSN();
                        Console.WriteLine("SN:{0} don't support ActionCommand or PTP", SN);
                        bIsAllDevSupport = false;
                    }
                }
                else
                {
                    string SN = lstDevPtr[i].GetDeviceInfo().GetSN();
                    Console.WriteLine("SN:{0} don't support ActionCommand or PTP", SN);
                    bIsAllDevSupport = false;
                }
            }

            if (!bIsAllDevSupport)
            {
                throw new CGalaxyException((int)GX_STATUS_LIST.GX_STATUS_ERROR, "not all cam support ActionCommand and PTP");
            }
            else
            {
                Console.WriteLine("successful check, all device support ActionCommand and PTP");
            }
        }

        // 设置相机参数并开采
        static void SetCamParametersAndStartAcquisition(List<IGXDevice> lstDevPtr
            , List<IGXFeatureControl> lstRemoteFeature
            , List<IGXStream> lstStream)
        {
            Console.WriteLine("setting cam ActionCommand parameters");

            for (int i = 0; i < lstRemoteFeature.Count; i++)
            {
                // 加载相机默认参数组
                lstRemoteFeature[i].GetEnumFeature("UserSetSelector").SetValue("Default");

                lstRemoteFeature[i].GetCommandFeature("UserSetLoad").Execute();

                // 开启触发模式
                lstRemoteFeature[i].GetEnumFeature("TriggerMode").SetValue("On");

                // 触发源设为Action0
                lstRemoteFeature[i].GetEnumFeature("TriggerSource").SetValue("Action0");

                // 设置相机ActionCommand参数
                lstRemoteFeature[i].GetIntFeature("ActionDeviceKey").SetValue(1);

                lstRemoteFeature[i].GetIntFeature("ActionGroupKey").SetValue(1);

                lstRemoteFeature[i].GetIntFeature("ActionGroupMask").SetValue(0xFFFFFFFF);

                // 开采
                lstStream[i].StartGrab();

                lstRemoteFeature[i].GetCommandFeature("AcquisitionStart").Execute();
            }

            Console.WriteLine("setting success");
        }

        // 演示ActionCommand命令
        static void ShowActionCommand(List<IGXDevice> lstDevPtr
            , List<IGXStream> lstStream
            , ref IntPtr pBuff)
        {
            Console.WriteLine("demonstrate ActionCommand function");

            UInt32 DeviceKey = 1;
            UInt32 GroupKey = 1;
            UInt32 GroupMask = 0xffffffff;
            string SpecialIP = "";
            UInt32 NumResult = (UInt32)lstDevPtr.Count;
            GX_GIGE_ACTION_COMMAND_RESULT[] Result = new GX_GIGE_ACTION_COMMAND_RESULT[NumResult];
            int size = Marshal.SizeOf(typeof(GX_GIGE_ACTION_COMMAND_RESULT)) * (int)NumResult;
            pBuff = Marshal.AllocHGlobal(size);

            // pBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
            string IP = "255.255.255.255";

            IGXFactory.GetInstance().GigEIssueActionCommand(DeviceKey, GroupKey, GroupMask
                , IP, SpecialIP, 500, ref NumResult, pBuff);

            for (int i = 0; i < NumResult; i++)
            {
                IntPtr Ptr = new IntPtr(pBuff.ToInt64() + Marshal.SizeOf(typeof(GX_GIGE_ACTION_COMMAND_RESULT)) * i);
                Result[i] = (GX_GIGE_ACTION_COMMAND_RESULT)Marshal.PtrToStructure(Ptr, typeof(GX_GIGE_ACTION_COMMAND_RESULT));
            }

            // 打印ack
            for (UInt32 i = 0; i < NumResult; i++)
            {
                Console.WriteLine("Ack Return ip:{0}, status:{1}"
                    , System.Text.Encoding.UTF8.GetString(Result[i].DeviceAddress).TrimEnd('\0')
                    , Result[i].Status);
            }

            // 获取图像
            for (int i = 0; i < lstDevPtr.Count; i++)
            {
                IFrameData Image = null;
                Image = lstStream[i].DQBuf(1000);

                Console.WriteLine("SN:{0} get image success, image status:{1}"
                    , lstDevPtr[i].GetDeviceInfo().GetSN()
                    , ((Image.GetStatus() == GX_FRAME_STATUS_LIST.GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame"));

                lstStream[i].QBuf(Image);
            }
        }

        // 演示ScheduledActionCommand命令
        static void ShowScheduledActionCommand(List<IGXDevice> lstDevPtr
            , List<IGXFeatureControl> lstRemoteFeature
            , List<IGXStream> lstStream
            , ref IntPtr pBuff)
        {
            UInt32 DeviceKey = 1;
            UInt32 GroupKey = 1;
            UInt32 GroupMask = 0xffffffff;
            string SpecialIP = "";
            UInt32 NumResult = (UInt32)lstDevPtr.Count;
            GX_GIGE_ACTION_COMMAND_RESULT[] Result = new GX_GIGE_ACTION_COMMAND_RESULT[NumResult];

            // pBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
            string IP = "255.255.255.255";

            // 设置相机PTP参数
            Console.WriteLine("setting cam PTP parameters");

            for (int i = 0; i < lstRemoteFeature.Count; i++)
            {
                // 打开相机PTP功能
                lstRemoteFeature[i].GetBoolFeature("PtpEnable").SetValue(true);
            }

            // 首先应该等待相机分配角色，需要时间约8s，循环读取PtpStatus，直到值为"Master"或"Slave"时，角色分配完成
            // 然后进行时间校准，精度到1μs内需要时间约1~2min，循环设置Slave相机的PtpDataSetLatch，并读取PtpOffsetFromMaster，
            // 即可获得Slave相对于Master的时间偏差，当PtpOffsetFromMaster的绝对值小于用户期望的时间精度，时间校准完成
            string Cam0PtpStatus = lstRemoteFeature[0].GetEnumFeature("PtpStatus").GetValue();

            int Loops = 0;
            bool bStatusOK = (Cam0PtpStatus.Equals("Master") || (Cam0PtpStatus.Equals("Slave")));

            while (!bStatusOK && Loops < 8)
            {
                Thread.Sleep(1000);
                Loops++;

                Cam0PtpStatus = lstRemoteFeature[0].GetEnumFeature("PtpStatus").GetValue();

                bStatusOK = (Cam0PtpStatus.Equals("Master") || (Cam0PtpStatus.Equals("Slave")));
            }

            if (!bStatusOK)
            {
                throw new CGalaxyException((int)GX_STATUS_LIST.GX_STATUS_ERROR, "PTP time calibration timeout");
            }

            Console.WriteLine("setting success");

            // 演示ScheduledActionCommand命令
            Console.WriteLine("demonstrate ScheduledActionCommand function");

            // 获取相机当前时间戳，单位ns，计划5s后相机采一张图像
            lstRemoteFeature[0].GetCommandFeature("TimestampLatch").Execute();
            Int64 TimeStamp = lstRemoteFeature[0].GetIntFeature("TimestampLatchValue").GetValue();
            TimeStamp += 5000000000;

            IGXFactory.GetInstance().GigEIssueScheduledActionCommand(DeviceKey, GroupKey, GroupMask
                , (UInt64)TimeStamp, IP, SpecialIP, 500, ref NumResult, pBuff);

            // 等待相机执行
            Thread.Sleep(5000);

            for (int i = 0; i < NumResult; i++)
            {
                IntPtr Ptr = new IntPtr(pBuff.ToInt64() + Marshal.SizeOf(typeof(GX_GIGE_ACTION_COMMAND_RESULT)) * i);
                Result[i] = (GX_GIGE_ACTION_COMMAND_RESULT)Marshal.PtrToStructure(Ptr, typeof(GX_GIGE_ACTION_COMMAND_RESULT));
            }

            // 打印ack
            for (UInt32 i = 0; i < NumResult; i++)
            {
                Console.WriteLine("Ack Return ip:{0}, status:{1}"
                    , System.Text.Encoding.UTF8.GetString(Result[i].DeviceAddress).TrimEnd('\0')
                    , Result[i].Status);
            }

            // 获取图像
            for (int i = 0; i < lstDevPtr.Count; i++)
            {
                IFrameData Image = null;
                Image = lstStream[i].DQBuf(1000);

                Console.WriteLine("SN:{0} get image success, image status:{1}"
                    , lstDevPtr[i].GetDeviceInfo().GetSN()
                    , ((Image.GetStatus() == GX_FRAME_STATUS_LIST.GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame"));

                lstStream[i].QBuf(Image);
            }
        }

        // 停止采集并关闭相机
        static void StopAcquisitionAndCloseCam(List<IGXDevice> lstDevPtr
            , List<IGXFeatureControl> lstRemoteFeature
            , List<IGXStream> lstStream)
        {
            for (int i = 0; i < lstRemoteFeature.Count; i++)
            {
                try
                {
                    lstRemoteFeature[i].GetCommandFeature("AcquisitionStop").Execute();
                }
                catch (Exception e)
                {
                    Console.WriteLine("cam idx:{0} stop acquisition fail!", i);
                    continue;
                }
            }

            for (int i = 0; i < lstStream.Count; i++)
            {
                try
                {
                    lstStream[i].Close();
                }
                catch (Exception e)
                {
                    Console.WriteLine("cam idx:{0} close stream fail!", i);
                    continue;
                }
            }

            for (int i = 0; i < lstDevPtr.Count; i++)
            {
                try
                {
                    lstDevPtr[i].Close();
                }
                catch (Exception e)
                {
                    Console.WriteLine("cam idx:{0} close device fail!", i);
                    continue;
                }
            }
        }

        static void Main()
        {
            List<IGXDevice> lstDevPtr = new List<IGXDevice>();
            List<IGXFeatureControl> lstRemoteFeature = new List<IGXFeatureControl>();
            List<IGXStream> lstStream = new List<IGXStream>();
            IntPtr pBuff = IntPtr.Zero;

            try
            {
                IGXFactory.GetInstance().Init();

                List<IGXDeviceInfo> lstDevInfo = new List<IGXDeviceInfo>();

                // 枚举网络相机设备
                IGXFactory.GetInstance().UpdateAllDeviceListEx((ulong)GX_TL_TYPE_LIST.GX_TL_TYPE_GEV, 1000, lstDevInfo);

                if (lstDevInfo.Count < 1)
                {
                    throw new CGalaxyException((int)GX_STATUS_LIST.GX_STATUS_ERROR, "Gige device less than 1!");
                }

                Console.WriteLine("open device");

                foreach (IGXDeviceInfo info in lstDevInfo)
                {
                    // 通过SN打开所有枚举到的网络相机设备
                    IGXDevice objDevPtr = IGXFactory.GetInstance().OpenDeviceBySN(info.GetSN(), GX_ACCESS_MODE.GX_ACCESS_EXCLUSIVE);
                    lstDevPtr.Add(objDevPtr);

                    // 获取相机属性控制对象
                    IGXFeatureControl objRemoteFeature = objDevPtr.GetRemoteFeatureControl();
                    lstRemoteFeature.Add(objRemoteFeature);

                    // 流层对象
                    uint StreamCount = objDevPtr.GetStreamCount();

                    if (StreamCount > 0)
                    {
                        IGXStream objStream = objDevPtr.OpenStream(0);
                        lstStream.Add(objStream);
                    }
                    else
                    {
                        throw new CGalaxyException((int)GX_STATUS_LIST.GX_STATUS_ERROR, "Not find stream!");
                    }

                    Console.WriteLine("<Model Name:{0}> <Serial Number:{1}>"
                        , info.GetModelName(), info.GetSN());
                }

                // 检查是否所有相机都支持ActionCommand和ptp功能
                CheckCamParameters(lstDevPtr, lstRemoteFeature);

                // 设置相机参数并开采
                SetCamParametersAndStartAcquisition(lstDevPtr, lstRemoteFeature, lstStream);

                // 演示ActionCommand命令
                ShowActionCommand(lstDevPtr, lstStream, ref pBuff);

                // 演示ScheduledActionCommand命令
                ShowScheduledActionCommand(lstDevPtr, lstRemoteFeature, lstStream, ref pBuff);

                // 停止采集并关闭相机
                StopAcquisitionAndCloseCam(lstDevPtr, lstRemoteFeature, lstStream);

                IGXFactory.GetInstance().Uninit();
            }
            catch (CGalaxyException e)
            {
                Console.WriteLine("<Get Galaxy Exception:{0}> <{1}>"
                    , e.GetErrorCode(), e.Message);

                // 停止采集并关闭相机
                StopAcquisitionAndCloseCam(lstDevPtr, lstRemoteFeature, lstStream);

                IGXFactory.GetInstance().Uninit();
            }
            catch (Exception e)
            {
                Console.WriteLine("<Get Unknow Error:{0}>"
                    , e.Message);

                IGXFactory.GetInstance().Uninit();
            }

            if (pBuff != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(pBuff);
            }

            Console.WriteLine("App exit!");
            Console.Read();

            return;
        }
    }
}