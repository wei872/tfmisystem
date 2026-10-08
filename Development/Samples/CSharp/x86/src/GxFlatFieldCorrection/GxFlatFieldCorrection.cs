using GxFlatFieldCorrection;
using GxIAPINET;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;


namespace GxFlatFieldCorrection
{
    internal class CGxFlatFieldCorrection
    {
        /// <summary>
        /// 执行平场校正流程
        /// </summary>
        public void FlatFieldCorrection()
        {
            IGXFactory objIGXFactory = null;                            ///<Factory对像
            IGXDevice objIGXDevice = null;                              ///<设备对像
            IGXStream objIGXStream = null;                              ///<流对像
            IGXFeatureControl objIGXRemoteFeatureControl = null;        ///<远端设备属性控制器对像
            bool bOpenSevice = false;                                   ///<设备是否打开

            try
            {
                //初始化设备库
                objIGXFactory = IGXFactory.GetInstance();
                objIGXFactory.Init();

                //枚举相机设备
                List<IGXDeviceInfo> listGXDeviceInfo = new List<IGXDeviceInfo>();
                objIGXFactory.UpdateDeviceList(1000, listGXDeviceInfo);

                // 判断当前连接设备个数
                if (listGXDeviceInfo.Count <= 0)
                {
                    Console.WriteLine("<No device!>");
                    Console.WriteLine("<Press any key to end...>");
                    Console.ReadKey();
                    return;
                }

                //通过SN打开相机设备
                objIGXDevice = objIGXFactory.OpenDeviceBySN(listGXDeviceInfo[0].GetSN(), GX_ACCESS_MODE.GX_ACCESS_EXCLUSIVE);
                bOpenSevice = true;

                //获取相机属性控制对象
                objIGXRemoteFeatureControl = objIGXDevice.GetRemoteFeatureControl();
                if (objIGXDevice.GetStreamCount() > 0)
                {
                    objIGXStream = objIGXDevice.OpenStream(0);
                }
                else
                {
                    if (bOpenSevice)
                    {
                        //关闭相机设备
                        objIGXDevice.Close();
                    }

                    //关闭设备库
                    objIGXFactory.Uninit();

                    Console.WriteLine("<Not find stream, App exit!>");
                    Console.WriteLine("<Press any key to end...>");
                    Console.ReadKey();
                    return;
                }

                //加载默认参数组
                objIGXRemoteFeatureControl.GetEnumFeature("UserSetSelector").SetValue("Default");
                objIGXRemoteFeatureControl.GetCommandFeature("UserSetLoad").Execute();

                Console.WriteLine("***********************************************");
                Console.WriteLine("<Vendor Name:   {0}>", objIGXDevice.GetDeviceInfo().GetVendorName());
                Console.WriteLine("<Model Name:    {0}>", objIGXDevice.GetDeviceInfo().GetModelName());
                Console.WriteLine("<Serial Number: {0}>", objIGXDevice.GetDeviceInfo().GetSN());
                Console.WriteLine("***********************************************");

                //1. 创建FFC处理对象
                IFlatFieldCorrectionProcess objFFCProcess = IFlatFieldCorrectionProcess.CreateFlatFieldCorrectionProcess(objIGXStream, objIGXRemoteFeatureControl);
                if (null == objFFCProcess)
                {
                    if (bOpenSevice)
                    {
                        // 关闭流
                        objIGXStream.Close();
                        // 关闭设备
                        objIGXDevice.Close();
                    }

                    // 反初始化接口
                    objIGXFactory.Uninit();

                    Console.WriteLine("<Create flat field correction process error, App exit!>");
                    Console.WriteLine("<Press any key to end...>");
                    Console.ReadKey();
                    return;
                }

                //2. 设置平场参数
                GX_FLAT_FIELD_CORRECTION_PARAM stFFCParam = new GX_FLAT_FIELD_CORRECTION_PARAM();
                stFFCParam.nFFCExpectedGray = 127;  // -1 ~255, -1标识用图像块儿的最大值 见说明书
                stFFCParam.nFFCFrameCount = 1;      // 1,2,4,8,16 标识融合帧数

                //通过调用 objIGXStreamFeatureControl.GetEnumFeature("FFCCoefficient").GetEnumValue()的值
                if (objIGXRemoteFeatureControl.IsImplemented("FFCCoefficient") &&
                    objIGXRemoteFeatureControl.IsReadable("FFCCoefficient"))
                {
                    stFFCParam.strCoefficient = objIGXRemoteFeatureControl.GetEnumFeature("FFCCoefficient").GetEnumValue().strCurSymbolic;
                }

                //通过调用 objIGXStreamFeatureControl.GetEnumFeature("FFCAccuracy").GetEnumValue()的值
                if (objIGXRemoteFeatureControl.IsImplemented("FFCAccuracy") &&
                    objIGXRemoteFeatureControl.IsReadable("FFCAccuracy"))
                {
                    stFFCParam.strAccuracy = objIGXRemoteFeatureControl.GetEnumFeature("FFCAccuracy").GetEnumValue().strCurSymbolic;
                }

                //通过调用 objIGXStreamFeatureControl.GetEnumFeature("FFCBlockSize").GetEnumValue()的值
                if (objIGXRemoteFeatureControl.IsImplemented("FFCBlockSize") &&
                    objIGXRemoteFeatureControl.IsReadable("FFCBlockSize"))
                {
                    stFFCParam.nFFCBlockSize = (Int32)objIGXRemoteFeatureControl.GetEnumFeature("FFCBlockSize").GetEnumValue().nCurValue;
                }

                stFFCParam.bFFCExpectedGray = true;
                objFFCProcess.SetFlatFieldCorrectionParam(ref stFFCParam);

                //3.计算平场矫正系数 false不采集暗场， true采集暗场,
                //仅FFC_SOFTCAL_SOFTUSE与FFC_SOFTCAL_DEVICEUSE_3140类型支持
                bool bCaculate = objFFCProcess.Calculate(false);
                if (!bCaculate)
                {
                    if (bOpenSevice)
                    {
                        // 关闭流
                        objIGXStream.Close();
                        // 关闭设备
                        objIGXDevice.Close();
                    }

                    // 反初始化
                    objIGXFactory.Uninit();

                    Console.WriteLine("<Calculate flat field correction error, App exit!>");
                    Console.WriteLine("<Press any key to end...>");
                    Console.ReadKey();
                    return;
                }

                Console.WriteLine("<Flat-field coefficients calculation completed successfully.>");

                //4.开启平场校正开关
                bool bEnableFFC = true;
                objFFCProcess.EnableFFC(bEnableFFC);
                Console.WriteLine("<Enable flat-field correction.>");

                //5.获取平场校正后的图像
                IBaseData objImgData = objFFCProcess.GetFFCImage();
                if (null != objImgData)
                {
                    if (bEnableFFC)
                    {
                        Console.WriteLine("<App get FFC Image Success!>");
                    }
                    else
                    {
                        Console.WriteLine("<App get normal Image Success!>");
                    }
                }

                //6. 可选保存平场矫正系数，加载平场矫正系数
                //注意FFC_DEVICECAL_DEVICEUSE类相机 当"FFCAccuracy" 设置为PixelLevel时 保存时间较长
                //当保存路径传空时，如果相机支持则平场系数将保存到相机内部, 返回值为是否成功

                // 建议以管理员身份启动程序，防止当前应用程序处于系统盘时因没有管理员权限导致保存失败！
                bool bSaveSuccess = objFFCProcess.SaveFFC("FlatFieldCorrectionProcess.ffc");
                bool bLoadSuccess = objFFCProcess.LoadFFC("FlatFieldCorrectionProcess.ffc");

                //关闭相机设备
                objIGXDevice.Close();
                //关闭设备库
                objIGXFactory.Uninit();
            }
            catch (CGalaxyException ex)
            {
                if (bOpenSevice)
                {
                    // 关闭流
                    objIGXStream.Close();
                    // 关闭设备
                    objIGXDevice.Close();
                }

                // 反初始化库
                objIGXFactory.Uninit();

                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                if (bOpenSevice)
                {
                    // 关闭流
                    objIGXStream.Close();
                    // 关闭设备
                    objIGXDevice.Close();
                }

                // 反初始化库
                objIGXFactory.Uninit();

                Console.WriteLine("<Unknown error>");
            }

            Console.WriteLine("<App exit! Press any key to end...>");
            Console.ReadKey();
            return;
        }
    }
}
