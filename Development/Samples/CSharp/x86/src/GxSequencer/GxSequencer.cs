using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Drawing;
using System.Linq;
using System.Text;
using System.Windows.Forms;
using System.Threading;
using System.IO;
using GxIAPINET;
using GxIAPINET.Sample.Common;
using System.Threading;

namespace GxSequencerSample
{
    public class GxSequencerSampleEvent
    {
        /// <summary>
        /// 回调函数,用于获取图像信息和显示图像
        /// </summary>
        /// <param name="obj">用户自定义传入参数</param>
        /// <param name="objIFrameData">图像信息对象</param>
        private void __CaptureCallbackPro(object objUserParam, IFrameData objIFrameData)
        {
            if (GX_FRAME_STATUS_LIST.GX_FRAME_STATUS_SUCCESS == objIFrameData.GetStatus())
            {
                Console.WriteLine("完整帧");
            }
            else
            {
                Console.WriteLine("残帧");
            }
        }

        public void __InitDevice()
        {
            IGXDevice m_objIGXDevice = null;  ///<设备对像
            IGXStream m_objIGXStream = null;  ///<流对像
            IGXFeatureControl m_objIGXFeatureControl = null;  ///<远端设备属性控制器对像
            IGXFactory m_objIGXFactory = null;  ///<Factory对像
            IGXFeatureControl m_objIGXStreamFeatureControl = null; ///<流层属性控制器对象

            try 
            {
                m_objIGXFactory = IGXFactory.GetInstance();
                m_objIGXFactory.Init();

                // 枚举设备
                List<IGXDeviceInfo> listGXDeviceInfo = new List<IGXDeviceInfo>();

                m_objIGXFactory.UpdateDeviceList(1000, listGXDeviceInfo);

                // 判断当前连接设备个数
                if (listGXDeviceInfo.Count <= 0)
                {
                    MessageBox.Show("未发现设备!");
                    return;
                }

                //打开列表第一个设备
                m_objIGXDevice = m_objIGXFactory.OpenDeviceBySN(listGXDeviceInfo[0].GetSN(), GX_ACCESS_MODE.GX_ACCESS_EXCLUSIVE);

                // 获取远端属性控制器
                m_objIGXFeatureControl = m_objIGXDevice.GetRemoteFeatureControl();

                //打开流
                if (null != m_objIGXDevice)
                {
                    m_objIGXStream = m_objIGXDevice.OpenStream(0);
                    m_objIGXStreamFeatureControl = m_objIGXStream.GetFeatureControl();
                }

                //开启采集流通道
                if (null != m_objIGXStream)
                {
                    //RegisterCaptureCallback第一个参数属于用户自定参数(类型必须为引用
                    //类型)，若用户想用这个参数可以在委托函数中进行使用
                    m_objIGXStream.RegisterCaptureCallback(this, __CaptureCallbackPro);
                }

                // 加载默认参数组
                m_objIGXFeatureControl.GetEnumFeature("UserSetSelector").SetValue("Default");
                m_objIGXFeatureControl.GetCommandFeature("UserSetLoad").Execute();

                /**配置序列组*/
                {
                    // 关闭序列模式
                    m_objIGXFeatureControl.GetEnumFeature("SequencerMode").SetValue("Off");

                    // 打开序列配置模式
                    m_objIGXFeatureControl.GetEnumFeature("SequencerConfigurationMode").SetValue("On");

                    /**配置第一组序列*/
                    m_objIGXFeatureControl.GetIntFeature("SequencerSetSelector").SetValue(0);
                    // 设置曝光时间
                    m_objIGXFeatureControl.GetFloatFeature("ExposureTime").SetValue(5000);
                    // 设置增益
                    m_objIGXFeatureControl.GetFloatFeature("Gain").SetValue(2);
                    // 设置Gamma
                    m_objIGXFeatureControl.GetEnumFeature("GammaMode").SetValue("User");
                    m_objIGXFeatureControl.GetBoolFeature("GammaEnable").SetValue(true);
                    m_objIGXFeatureControl.GetFloatFeature("Gamma").SetValue(1);
                    // 保存第一组序列
                    m_objIGXFeatureControl.GetCommandFeature("SequencerSetSave").Execute();

                    /**配置第二组序列*/
                    m_objIGXFeatureControl.GetIntFeature("SequencerSetSelector").SetValue(1);
                    // 设置曝光时间
                    m_objIGXFeatureControl.GetFloatFeature("ExposureTime").SetValue(8000);
                    // 设置增益
                    m_objIGXFeatureControl.GetFloatFeature("Gain").SetValue(5);
                    // 设置Gamma
                    m_objIGXFeatureControl.GetEnumFeature("GammaMode").SetValue("User");
                    m_objIGXFeatureControl.GetBoolFeature("GammaEnable").SetValue(true);
                    m_objIGXFeatureControl.GetFloatFeature("Gamma").SetValue(2);
                    // 保存第二组序列
                    m_objIGXFeatureControl.GetCommandFeature("SequencerSetSave").Execute();

                    /**配置第三组序列*/
                    m_objIGXFeatureControl.GetIntFeature("SequencerSetSelector").SetValue(2);
                    // 设置曝光时间
                    m_objIGXFeatureControl.GetFloatFeature("ExposureTime").SetValue(10000);
                    // 设置增益
                    m_objIGXFeatureControl.GetFloatFeature("Gain").SetValue(10);
                    // 设置Gamma
                    m_objIGXFeatureControl.GetEnumFeature("GammaMode").SetValue("User");
                    m_objIGXFeatureControl.GetBoolFeature("GammaEnable").SetValue(true);
                    m_objIGXFeatureControl.GetFloatFeature("Gamma").SetValue(3);
                    // 保存第三组序列
                    m_objIGXFeatureControl.GetCommandFeature("SequencerSetSave").Execute();

                    /**配置第四组序列*/
                    m_objIGXFeatureControl.GetIntFeature("SequencerSetSelector").SetValue(3);
                    // 设置曝光时间
                    m_objIGXFeatureControl.GetFloatFeature("ExposureTime").SetValue(15000);
                    // 设置增益
                    m_objIGXFeatureControl.GetFloatFeature("Gain").SetValue(14);
                    // 设置Gamma
                    m_objIGXFeatureControl.GetEnumFeature("GammaMode").SetValue("User");
                    m_objIGXFeatureControl.GetBoolFeature("GammaEnable").SetValue(true);
                    m_objIGXFeatureControl.GetFloatFeature("Gamma").SetValue(4);
                    // 保存第四组序列
                    m_objIGXFeatureControl.GetCommandFeature("SequencerSetSave").Execute();

                    // 关闭序列配置模式
                    m_objIGXFeatureControl.GetEnumFeature("SequencerConfigurationMode").SetValue("Off");
                }

                // 设置触发模式为软触发
                m_objIGXFeatureControl.GetEnumFeature("TriggerSelector").SetValue("FrameStart");
                m_objIGXFeatureControl.GetEnumFeature("TriggerSource").SetValue("Software");
                m_objIGXFeatureControl.GetEnumFeature("TriggerMode").SetValue("On");

                // 打开序列模式（打开触发模式后序列模式才可设）
                m_objIGXFeatureControl.GetEnumFeature("SequencerMode").SetValue("On");

                // 开始采集
                m_objIGXStream.StartGrab();
                m_objIGXFeatureControl.GetCommandFeature("AcquisitionStart").Execute();

                // 软触发采集
                for (int i32Count = 4; i32Count != 0; i32Count--)
                {
                    m_objIGXFeatureControl.GetCommandFeature("TriggerSoftware").Execute();
                    Thread.Sleep(1000);
                }

                // 停止采集
                m_objIGXFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                m_objIGXStream.StopGrab();
                m_objIGXStream.UnregisterCaptureCallback();
                m_objIGXDevice.Close();
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.Message);
            }

            m_objIGXFactory.Uninit();
        }
    }
}