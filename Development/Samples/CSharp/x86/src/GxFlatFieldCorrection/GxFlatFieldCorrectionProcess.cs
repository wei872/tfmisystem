using GxIAPINET;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace GxFlatFieldCorrection
{
    /// <summary>
    /// 平场校正参数
    /// </summary>
    internal struct GX_FLAT_FIELD_CORRECTION_PARAM
    {
        public Int32 nFFCExpectedGray;             ///< FFC expected gray value 
        public Int32 nFFCFrameCount;               ///< FFC Frame Count
        public string strCoefficient;              ///< FFC Coefficient
        public string strAccuracy;                 ///< FFC Accuracy
        public Int32 nFFCBlockSize;                ///< block size
        public bool bFFCExpectedGray;              ///< Enable FFC expected gray value
    };

    /// <summary>
    /// 平场类型
    /// </summary>
    internal enum FFC_TYPE
    {
        FFC_UNKNOWN = -1,	                      ///< 未定义
        FFC_SOFTCAL_SOFTUSE = 0,	              ///< 第一类相机，相机本身不支持平场需通过软件实现
        FFC_SOFTCAL_DEVICEUSE = 1,                ///< 第二类相机，相机本身不能计算平场系数需要依靠软件计算（计算时仅需亮场），但可以应用平场系数。
        FFC_SOFTCAL_DEVICEUSE_3140 = 2,           ///< 第二类相机，相机本身不能计算平场系数需要依靠软件计算（计算时需要亮场，可选暗场），但可以应用平场系数。
        FFC_DEVICECAL_DEVICEUSE = 3,              ///< 第三类相机，相机本身可以计算平场系数并应用系数
    };

    /// <summary>
    /// 平场校正类（父类）
    /// </summary>
    internal class IFlatFieldCorrectionProcess
    {
        /// <summary>
        /// 创建平场对象
        /// </summary>
        /// <param name="objDevStream">设备流对象</param>
        /// <param name="objDevRemoteFeatureControl">设备远端属性控制对象</param>
		/// <returns>平场校正对象</returns>
        public static IFlatFieldCorrectionProcess CreateFlatFieldCorrectionProcess(IGXStream objDevStream, IGXFeatureControl objDevRemoteFeatureControl)
        {
            if (null == m_objFlatFieldCorrectionProcess)
            {
                FFC_TYPE emFFC = __GetFFCType(objDevRemoteFeatureControl);
                switch (emFFC)
                {
                    case FFC_TYPE.FFC_SOFTCAL_SOFTUSE:
                        m_objFlatFieldCorrectionProcess = new CGXSoftCalSoftUseFFC(objDevStream, objDevRemoteFeatureControl);
                        break;
                    case FFC_TYPE.FFC_SOFTCAL_DEVICEUSE:
                    case FFC_TYPE.FFC_SOFTCAL_DEVICEUSE_3140:
                        m_objFlatFieldCorrectionProcess = new CGXSoftCalDeviceUseFFC(objDevStream, objDevRemoteFeatureControl);
                        break;
                    case FFC_TYPE.FFC_DEVICECAL_DEVICEUSE:
                        m_objFlatFieldCorrectionProcess = new CGXDeviceCalDeviceUseFFC(objDevStream, objDevRemoteFeatureControl);
                        break;
                    case FFC_TYPE.FFC_UNKNOWN:
                        break;
                }
                return m_objFlatFieldCorrectionProcess;
            }
            else
            {
                return m_objFlatFieldCorrectionProcess;
            }
        }

        /// <summary>
        /// 设置平场参数
        /// </summary>
        /// <param name="stFFCParam">平场校正参数</param>
        public virtual void SetFlatFieldCorrectionParam(ref GX_FLAT_FIELD_CORRECTION_PARAM stFFCParam) { }

        /// <summary>
        /// 获取平场校正后的图像
        /// </summary>
		/// <returns>图像数据对象</returns>
        public virtual IBaseData GetFFCImage()
        {
            return null;
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
		/// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：保存成功  false：保存失败</returns>
        public virtual bool SaveFFC(string strFFCPath)
        {
            return false;
        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
		/// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：加载成功  false：加载失败</returns>
        public virtual bool LoadFFC(string strFFCPath)
        {
            return false;
        }

        /// <summary>
        /// 计算平场矫正系数
        /// </summary>
        /// <param name="bNeedDark">是否支持暗场</param>
		/// <returns>true：计算成功  false：计算失败</returns>
        public virtual bool Calculate(bool bNeedDark)
        {
            IFrameData objImgData = null;
            bool bStartGrab = false;
            try
            {
                //开启流层采集
                m_objDevStream.StartGrab();
                //开启相机采集
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                bStartGrab = true;

                objImgData = m_objDevStream.DQBuf(20000);

                GX_FLAT_FIELD_CORRECTION_PARAMETER stFFCParam = new GX_FLAT_FIELD_CORRECTION_PARAMETER();
                stFFCParam.pBrightBuf = objImgData.GetBuffer();

                FFC_TYPE emFFCType = __GetFFCType(m_objDevRemoteFeatureControl);
                if (FFC_TYPE.FFC_SOFTCAL_DEVICEUSE == emFFCType)
                {
                    stFFCParam.pDarkBuf = IntPtr.Zero;  //该类相机不支持暗场直接设置为空
                }
                else
                {
                    if (bNeedDark)
                    {
                        Console.WriteLine("<Dark field acquisition will start. Please cover the lens and press any key to continue.>");

                        //确保得到的是新图 s7588
                        Thread.Sleep(1000);
                        stFFCParam.pDarkBuf = m_objDevStream.GetImage(20000).GetBuffer();
                    }
                    else
                    {
                        stFFCParam.pDarkBuf = IntPtr.Zero;
                    }
                }

                stFFCParam.emPixelFormat = objImgData.GetPixelFormat();
                stFFCParam.nImgWid = (int)objImgData.GetWidth();
                stFFCParam.nImgHei = (int)objImgData.GetHeight();

                stFFCParam.nFFCBlockSize = m_nBlockSize;
                stFFCParam.nFFCExpectedGray = m_nExpectedGray;

                //获取平场系数大小分配内存
                int nFFCCoefficientsSize = m_objDevFlatFieldCorrection.GetCoefficientsSize(ref stFFCParam);
                if (IntPtr.Zero != m_pFFCCoefficientBuffer)
                {
                    Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                    m_pFFCCoefficientBuffer = IntPtr.Zero;
                    m_nFFCCoefficientSize = 0;
                }

                m_pFFCCoefficientBuffer = Marshal.AllocHGlobal(nFFCCoefficientsSize);
                m_nFFCCoefficientSize = nFFCCoefficientsSize;

                //通过算法接口计算平场系数
                m_objDevFlatFieldCorrection.Calculate(ref stFFCParam, m_pFFCCoefficientBuffer, ref nFFCCoefficientsSize);

                m_objDevStream.QBuf(objImgData);

                //停采
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                m_objDevStream.StopGrab();
                return true;
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);

                if (IntPtr.Zero != m_pFFCCoefficientBuffer)
                {
                    Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                    m_pFFCCoefficientBuffer = IntPtr.Zero;
                    m_nFFCCoefficientSize = 0;
                }

                if (null != objImgData)
                {
                    m_objDevStream.QBuf(objImgData);
                }

                if (bStartGrab)
                {
                    //停采
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }

                return false;
            }
            catch (Exception ex)
            {
                if (IntPtr.Zero != m_pFFCCoefficientBuffer)
                {
                    Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                    m_pFFCCoefficientBuffer = IntPtr.Zero;
                    m_nFFCCoefficientSize = 0;
                }

                if (null != objImgData)
                {
                    m_objDevStream.QBuf(objImgData);
                }

                if (bStartGrab)
                {
                    //停采
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }

                Console.WriteLine("<Unknown error>");
                return false;
            }
        }

        /// <summary>
        /// 开启平场校正开关
        /// </summary>
        /// <param name="bEnableFFC">平场校正功能是否使能</param>
        public virtual void EnableFFC(bool bEnableFFC)
        {
            try
            {
                string strEnableFFC = bEnableFFC ? "On" : "Off";
                if (m_objDevRemoteFeatureControl.IsImplemented("FlatFieldCorrection") &&
                    m_objDevRemoteFeatureControl.IsWritable("FlatFieldCorrection"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FlatFieldCorrection").SetValue(strEnableFFC);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
                return;
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
                return;
            }
        }

        /// <summary>
        /// 平场校正构造函数
        /// </summary>
        /// <param name="objDevStream">设备流对象</param>
        /// <param name="objDevRemoteFeatureControl">设备远端属性控制对象</param>
        public IFlatFieldCorrectionProcess(IGXStream objDevStream, IGXFeatureControl objDevRemoteFeatureControl)
        {
            m_objDevStream = objDevStream;
            m_objDevRemoteFeatureControl = objDevRemoteFeatureControl;
            m_objDevFlatFieldCorrection = IGXFactory.GetInstance().CreateFlatFieldCorrection();
        }

        /// <summary>
        /// 析构函数
        /// </summary>
        ~IFlatFieldCorrectionProcess()
        {
            if (IntPtr.Zero != m_pFFCCoefficientBuffer)
            {
                Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                m_pFFCCoefficientBuffer = IntPtr.Zero;
                m_nFFCCoefficientSize = 0;
            }
        }

        /// <summary>
        /// 设置矫正精度
        /// </summary>
        /// <param name="nBlockSize">平场校正块大小</param>
        protected void __SetBlockSize(Int32 nBlockSize)
        {
            try
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCBlockSize") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCBlockSize"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FFCBlockSize").SetEnumValue(nBlockSize);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
            m_nBlockSize = nBlockSize;
        }

        /// <summary>
        /// 设置期望灰度值
        /// </summary>
        /// <param name="nExpectedGray">期望灰度值</param>
        protected void __SetExpectedGray(Int32 nExpectedGray)
        {
            try
            {
                bool bSupport = m_objDevRemoteFeatureControl.IsImplemented("FFCExpectedGray");
                if (bSupport)
                {
                    m_objDevRemoteFeatureControl.GetIntFeature("FFCExpectedGray").SetValue(nExpectedGray);
                }
                else
                {
                    bSupport = m_objDevRemoteFeatureControl.IsImplemented("FFCExpectGray");
                    if (bSupport)
                    {
                        m_objDevRemoteFeatureControl.GetIntFeature("FFCExpectGray").SetValue(nExpectedGray);
                    }
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
            m_nExpectedGray = nExpectedGray;
        }

        /// <summary>
        /// 设置融合帧数
        /// </summary>
        /// <param name="nFrameCount">帧数</param>
        protected void __SetFrameCount(Int32 nFrameCount)
        {
            try
            {
                m_objDevFlatFieldCorrection.SetFrameCount((ushort)nFrameCount);

                string strFrameCount = "FFCFrameCount_" + nFrameCount.ToString();
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCFrameCount") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCFrameCount"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FFCFrameCount").SetValue(strFrameCount);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
        }

        /// <summary>
        /// 判断相机属于那种类型
        /// </summary>
        /// <param name="objDevRemoteFeatureControl">设备远端设备属性控制对象</param>
		/// <returns>平场校正类型</returns>
        protected static FFC_TYPE __GetFFCType(IGXFeatureControl objDevRemoteFeatureControl)
        {
            try
            {
                bool bIsImplemented = objDevRemoteFeatureControl.IsImplemented("ShadingCorrectionMode");
                if (!bIsImplemented)
                {
                    return FFC_TYPE.FFC_SOFTCAL_SOFTUSE;
                }
                else
                {
                    string strShadingCorrectionMode = objDevRemoteFeatureControl.GetEnumFeature("ShadingCorrectionMode").GetValue();
                    if ("FlatFieldCorrection" == strShadingCorrectionMode)
                    {
                        return FFC_TYPE.FFC_SOFTCAL_DEVICEUSE_3140;
                    }
                    else if ("TailorFlatFieldCorrection" == strShadingCorrectionMode)
                    {
                        return FFC_TYPE.FFC_SOFTCAL_DEVICEUSE;
                    }
                    else if ("DeviceFlatFieldCorrection" == strShadingCorrectionMode)
                    {
                        return FFC_TYPE.FFC_DEVICECAL_DEVICEUSE;
                    }
                    else
                    {
                        Console.WriteLine("<Unknown Device>");
                        return FFC_TYPE.FFC_UNKNOWN;
                    }
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
                return FFC_TYPE.FFC_UNKNOWN;
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown Device>");
                return FFC_TYPE.FFC_UNKNOWN;
            }
        }

        /// <summary>
        /// 设置FFCAccuracy
        /// </summary>
        /// <param name="strFFCAccuracy">平场校正精确度</param>
        protected void __SetFFCAccuracy(string strFFCAccuracy)
        {
            try
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCAccuracy") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCAccuracy"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FFCAccuracy").SetValue(strFFCAccuracy);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
        }

        /// <summary>
        /// 设置设置期望灰度值使能
        /// </summary>
        /// <param name="bExpectedGrayEnable">期望灰度值是否使能</param>
        protected void __SetExpectedGrayEnable(bool bExpectedGrayEnable)
        {
            try
            {
                string strEnableFFC = bExpectedGrayEnable ? "On" : "Off";
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCExpectedGrayValueEnable") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCExpectedGrayValueEnable"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FFCExpectedGrayValueEnable").SetValue(strEnableFFC);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：保存成功  false：保存失败</returns>
        protected bool __SavePCFFC(string strFFCPath)
        {
            if ((0 == m_nFFCCoefficientSize) || (-1 == m_nFFCCoefficientSize))
            {
                Console.WriteLine("<Save FFC file {0}, FFCCoefficientSize is 0 or -1.>", strFFCPath);
                return false;
            }

            if (File.Exists(strFFCPath))
            {
                File.Delete(strFFCPath);
            }

            FileStream objFileStream = new FileStream(strFFCPath, FileMode.OpenOrCreate, FileAccess.ReadWrite);
            byte[] szBuffer = new byte[m_nFFCCoefficientSize];
            Marshal.Copy(m_pFFCCoefficientBuffer, szBuffer, 0, m_nFFCCoefficientSize);
            objFileStream.Write(szBuffer, 0, m_nFFCCoefficientSize);
            objFileStream.Close();

            Console.WriteLine("<Save FFC parameters to '{0}' file successfully.>", strFFCPath);
            return true;
        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：加载成功  false：加载失败</returns>
        protected bool __LoadPCFFC(string strFFCPath)
        {
            if (!File.Exists(strFFCPath))
            {
                Console.WriteLine("<open file {0} error.>", strFFCPath);
                return false;
            }

            //支持再清空
            if (IntPtr.Zero != m_pFFCCoefficientBuffer)
            {
                Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                m_pFFCCoefficientBuffer = IntPtr.Zero;
                m_nFFCCoefficientSize = 0;
            }

            //1.获取文件大小
            FileInfo fileInfo = new FileInfo(strFFCPath);
            m_nFFCCoefficientSize = (int)fileInfo.Length;

            //2.分配缓存
            m_pFFCCoefficientBuffer = Marshal.AllocHGlobal(m_nFFCCoefficientSize);

            //3.读取平场系数
            FileStream objFileStream = new FileStream(strFFCPath, FileMode.Open, FileAccess.Read);
            byte[] szFFCCoefficientBuffer = new byte[m_nFFCCoefficientSize];
            objFileStream.Read(szFFCCoefficientBuffer, 0, m_nFFCCoefficientSize);
            Marshal.Copy(szFFCCoefficientBuffer, 0, m_pFFCCoefficientBuffer, m_nFFCCoefficientSize);

            objFileStream.Close();

            Console.WriteLine("<Successfully loaded FFC configuration file {0}.>", strFFCPath);
            return true;
        }

        /// <summary>
        /// 设置平场校正系数选择
        /// </summary>
        /// <param name="strFFCCoefficient">平场校正系数</param>
        protected void __SetCoefficient(string strFFCCoefficient)
        {
            try
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCCoefficient") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCCoefficient"))
                {
                    m_objDevRemoteFeatureControl.GetEnumFeature("FFCCoefficient").SetValue(strFFCCoefficient);
                }
            }
            catch (CGalaxyException ex)
            {
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                Console.WriteLine("<Unknown error>");
            }
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：保存成功  false：保存失败</returns>
        protected bool __SaveDeviceFFC(string strFFCPath)
        {
            if ((!m_objDevRemoteFeatureControl.IsImplemented("FFCCoefficientsSize")) ||
                (!m_objDevRemoteFeatureControl.IsReadable("FFCCoefficientsSize")) ||
                (!m_objDevRemoteFeatureControl.IsImplemented("FFCValueAll")) ||
                (!m_objDevRemoteFeatureControl.IsWritable("FFCValueAll")))
            {
                Console.WriteLine("<The device does not support saving FFC parameters to a file.>");
                return false;
            }

            if (File.Exists(strFFCPath))
            {
                File.Delete(strFFCPath);
            }

            Int32 nFFCCoefficientSize = (Int32)m_objDevRemoteFeatureControl.GetIntFeature("FFCCoefficientsSize").GetValue();
            byte[] szFFCValue = new byte[nFFCCoefficientSize];

            m_objDevRemoteFeatureControl.GetRegisterFeature("FFCValueAll").GetBuffer(szFFCValue);

            FileStream objFileStream = new FileStream(strFFCPath, FileMode.OpenOrCreate, FileAccess.ReadWrite);
            objFileStream.Write(szFFCValue, 0, nFFCCoefficientSize);
            objFileStream.Close();

            Console.WriteLine("<Save FFC parameters to '{0}' file successfully.>", strFFCPath);
            return true;
        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
		/// <returns>true：加载成功  false：加载失败</returns>
        protected bool __LoadDeviceFFC(string strFFCPath)
        {
            if ((!m_objDevRemoteFeatureControl.IsImplemented("FFCCoefficientsSize")) ||
                (!m_objDevRemoteFeatureControl.IsReadable("FFCCoefficientsSize")) ||
                (!m_objDevRemoteFeatureControl.IsImplemented("FFCValueAll")) ||
                (!m_objDevRemoteFeatureControl.IsWritable("FFCValueAll")))
            {
                Console.WriteLine("<The device does not support loading FFC parameters to device.>");
                return false;
            }

            if (!File.Exists(strFFCPath))
            {
                Console.WriteLine("<open file {0} error.>", strFFCPath);
                return false;
            }

            //1.获取文件大小
            FileInfo fileInfo = new FileInfo(strFFCPath);
            int nFFCCoefficientSize = (int)fileInfo.Length;

            //2.分配缓存
            byte[] szFFCValue = new byte[nFFCCoefficientSize];

            //3.读取平场系数
            FileStream objFileStream = new FileStream(strFFCPath, FileMode.Open, FileAccess.Read);
            objFileStream.Read(szFFCValue, 0, nFFCCoefficientSize);


            m_objDevRemoteFeatureControl.GetRegisterFeature("FFCValueAll").SetBuffer(szFFCValue);
            objFileStream.Close();

            Console.WriteLine("<Successfully loaded FFC configuration file {0}.>", strFFCPath);
            return true;
        }

        /// 保护成员变量
        private static IFlatFieldCorrectionProcess m_objFlatFieldCorrectionProcess = null;
        protected IGXStream m_objDevStream = null;
        protected IGXFeatureControl m_objDevRemoteFeatureControl = null;
        protected IGXFlatFieldCorrection m_objDevFlatFieldCorrection = null;
        protected Int32 m_nBlockSize = 0;
        protected Int32 m_nFrameCount = 0;
        protected Int32 m_nExpectedGray = 0;
        protected Int32 m_nFFCCoefficientSize = 0;
        protected IntPtr m_pFFCCoefficientBuffer = IntPtr.Zero;
    }

    /// <summary>
    /// 第一类相机，相机本身不支持平场需通过软件实现
    /// </summary>
    internal class CGXSoftCalSoftUseFFC : IFlatFieldCorrectionProcess
    {
        /// <summary>
        /// 第一类相机，平场校正构造函数
        /// </summary>
        /// <param name="objDevStream">设备流对象</param>
        /// <param name="objDevRemoteFeatureControl">设备远端属性控制对象</param>
        public CGXSoftCalSoftUseFFC(IGXStream objDevStream, IGXFeatureControl objDevRemoteFeatureControl)
            : base(objDevStream, objDevRemoteFeatureControl)
        {
        }

        /// <summary>
        /// 设置平场参数
        /// </summary>
        /// <param name="stFFCParam">平场校正参数</param>
        public override void SetFlatFieldCorrectionParam(ref GX_FLAT_FIELD_CORRECTION_PARAM stFFCParam)
        {
            m_nBlockSize = -1;
            if (stFFCParam.bFFCExpectedGray)
            {
                __SetExpectedGray(stFFCParam.nFFCExpectedGray);
            }
            else
            {
                m_nExpectedGray = -1;
            }

            // 设置融合帧数
            __SetFrameCount(stFFCParam.nFFCFrameCount);
        }

        /// <summary>
        /// 获取平场校正后的图像
        /// </summary>
        /// <returns>图像数据</returns>
        public override IBaseData GetFFCImage()
        {
            bool bStartGrab = false;
            try
            {
                //开采
                m_objDevStream.StartGrab();
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                bStartGrab = true;

                //确保得到的是新图
                m_objDevStream.FlushQueue();
                Thread.Sleep(1000);

                IImageData objImgData = m_objDevStream.GetImage(20000);

                //如果用户启用平场则 应用平场系数
                if (m_bEnableFFC)
                {
                    m_objDevFlatFieldCorrection.FlatFieldCorrection(objImgData.GetBuffer(), objImgData.GetBuffer(),
                        GX_ACTUAL_BITS.GX_ACTUAL_BITS_8, (uint)objImgData.GetWidth(), (uint)objImgData.GetHeight(), m_pFFCCoefficientBuffer,
                        ref m_nFFCCoefficientSize);
                }

                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                m_objDevStream.StopGrab();

                return objImgData;
            }
            catch (CGalaxyException ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<Unknown error>");
            }

            return null;
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：保存成功  false：保存失败</returns>
        public override bool SaveFFC(string strFFCPath)
        {
            return __SavePCFFC(strFFCPath);
        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：加载成功  false：加载失败</returns>
        public override bool LoadFFC(string strFFCPath)
        {
            return __LoadPCFFC(strFFCPath);
        }

        /// <summary>
        /// 开启平场校正开关
        /// </summary>
        /// <param name="bEnableFFC">平场校正功能是否使能</param>
        public override void EnableFFC(bool bEnableFFC)
        {
            m_bEnableFFC = bEnableFFC;
        }

        private bool m_bEnableFFC = false;
    }

    /// <summary>
    /// 第二类相机，相机本身不能计算平场系数需要依靠软件计算
    /// </summary>
    internal class CGXSoftCalDeviceUseFFC : IFlatFieldCorrectionProcess
    {
        /// <summary>
        /// 第二类相机，平场校正构造函数
        /// </summary>
        /// <param name="objDevStream">设备流对象</param>
        /// <param name="objDevRemoteFeatureControl">设备远端属性控制对象</param>
        public CGXSoftCalDeviceUseFFC(IGXStream objDevStream, IGXFeatureControl objDevRemoteFeatureControl)
            : base(objDevStream, objDevRemoteFeatureControl)
        {
        }

        /// <summary>
        /// 设置平场参数
        /// </summary>
        /// <param name="stFFCParam">平场校正参数</param>
        public override void SetFlatFieldCorrectionParam(ref GX_FLAT_FIELD_CORRECTION_PARAM stFFCParam)
        {
            //1. 设置blocksize
            __SetBlockSize(stFFCParam.nFFCBlockSize);

            //2. 设置期望灰度值
            __SetExpectedGray(stFFCParam.nFFCExpectedGray);

            //3. 设置融合帧数
            __SetFrameCount(stFFCParam.nFFCFrameCount);

            //4. 设置期望灰度值使能
            __SetExpectedGrayEnable(stFFCParam.bFFCExpectedGray);
        }

        /// <summary>
        /// 获取平场校正后的图像
        /// </summary>
        /// <returns>图像数据对象</returns>
        public override IBaseData GetFFCImage()
        {
            bool bStartGrab = false;
            try
            {
                //若节点不可访问等抛出异常 直接打印错误日志
                byte[] szFFCValue = new byte[m_nFFCCoefficientSize];
                Marshal.Copy(m_pFFCCoefficientBuffer, szFFCValue, 0, m_nFFCCoefficientSize);
                long nLen = m_objDevRemoteFeatureControl.GetRegisterFeature("FFCValueAll").GetLength();
                m_objDevRemoteFeatureControl.GetRegisterFeature("FFCValueAll").SetBuffer(szFFCValue);

                // 开采
                m_objDevStream.StartGrab();
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                bStartGrab = true;

                //确保得到的是新图
                m_objDevStream.FlushQueue();
                Thread.Sleep(1000);

                IImageData objImgData = m_objDevStream.GetImage(20000);

                // 停采
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                m_objDevStream.StopGrab();

                return objImgData;
            }
            catch (CGalaxyException ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<Unknown error>");
            }

            return null;
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：保存成功  false：保存失败</returns>
        public override bool SaveFFC(string strFFCPath)
        {
            if (0 != strFFCPath.Length)
            {
                return __SaveDeviceFFC(strFFCPath);
            }
            else
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCFlashSave") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCFlashSave"))
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("FFCFlashSave").Execute();
                    return true;
                }
                else
                {
                    return false;
                }
            }
        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：加载成功  false：加载失败</returns>
        public override bool LoadFFC(string strFFCPath)
        {
            if (0 != strFFCPath.Length)
            {
                return __LoadDeviceFFC(strFFCPath);
            }
            else
            {
                if (!m_objDevRemoteFeatureControl.IsImplemented("FFCCoefficientsSize") ||
                    !m_objDevRemoteFeatureControl.IsReadable("FFCCoefficientsSize") ||
                    !m_objDevRemoteFeatureControl.IsImplemented("FFCFlashLoad") ||
                    !m_objDevRemoteFeatureControl.IsWritable("FFCFlashLoad") ||
                    !m_objDevRemoteFeatureControl.IsImplemented("FFCValueAll") ||
                    !m_objDevRemoteFeatureControl.IsWritable("FFCValueAll"))
                {
                    return false;
                }

                //支持再清空旧数据， 防止抛异常报错吧旧系数也没了
                if (IntPtr.Zero == m_pFFCCoefficientBuffer)
                {
                    Marshal.FreeHGlobal(m_pFFCCoefficientBuffer);
                    m_pFFCCoefficientBuffer = IntPtr.Zero;
                    m_nFFCCoefficientSize = 0;
                }

                m_nFFCCoefficientSize = (int)m_objDevRemoteFeatureControl.GetIntFeature("FFCCoefficientsSize").GetValue();
                m_pFFCCoefficientBuffer = Marshal.AllocHGlobal(m_nFFCCoefficientSize);

                m_objDevRemoteFeatureControl.GetCommandFeature("FFCFlashLoad").Execute();
                byte[] szFFCValue = new byte[m_nFFCCoefficientSize];
                m_objDevRemoteFeatureControl.GetRegisterFeature("FFCValueAll").GetBuffer(szFFCValue);
                Marshal.Copy(szFFCValue, 0, m_pFFCCoefficientBuffer, m_nFFCCoefficientSize);
                return true;
            }
        }
    }

    /// <summary>
    /// 第三类相机，相机本身可以计算平场系数并应用系数
    /// </summary>
    internal class CGXDeviceCalDeviceUseFFC : IFlatFieldCorrectionProcess
    {
        /// <summary>
        /// 第三类相机，平场校正构造函数
        /// </summary>
        /// <param name="objDevStream">设备流对象</param>
        /// <param name="objDevRemoteFeatureControl">设备远端属性控制对象</param>
        public CGXDeviceCalDeviceUseFFC(IGXStream objDevStream, IGXFeatureControl objDevRemoteFeatureControl)
            : base(objDevStream, objDevRemoteFeatureControl)
        {
        }

        /// <summary>
        /// 设置平场参数
        /// </summary>
        /// <param name="stFFCParam">平场校正参数</param>
        public override void SetFlatFieldCorrectionParam(ref GX_FLAT_FIELD_CORRECTION_PARAM stFFCParam)
        {
            //1. 设置blocksize
            __SetBlockSize(stFFCParam.nFFCBlockSize);

            //2. 设置期望灰度值
            __SetExpectedGray(stFFCParam.nFFCExpectedGray);

            //3. 设置融合帧数
            __SetFrameCount(stFFCParam.nFFCFrameCount);

            //4. 设置期望灰度值使能
            __SetExpectedGrayEnable(stFFCParam.bFFCExpectedGray);

            //5. 设置平场校正系数选择
            __SetCoefficient(stFFCParam.strCoefficient);

            //6. 设置算法精度
            __SetFFCAccuracy(stFFCParam.strAccuracy);
        }

        /// <summary>
        /// 计算平场矫正系数
        /// </summary>
        /// <param name="bNeedDark">是否支持暗场</param>
        /// <returns>true：计算成功  false：计算失败</returns>
        public override bool Calculate(bool bNeedDark)
        {
            bool bStartGrab = false;
            bool bCalculate = false;
            try
            {
                //开启流层采集
                m_objDevStream.StartGrab();
                //开启相机采集
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                bStartGrab = true;

                if (m_objDevRemoteFeatureControl.IsImplemented("FFCGenerate") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCGenerate"))
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("FFCGenerate").Execute();
                    bCalculate = true;
                }

                // 停采
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                m_objDevStream.StopGrab();
            }
            catch (CGalaxyException ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                bCalculate = false;
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                bCalculate = false;
                Console.WriteLine("<Unknown error>");
            }
            return bCalculate;
        }

        /// <summary>
        /// 获取平场校正后的图像
        /// </summary>
        /// <returns>图像数据对象</returns>
        public override IBaseData GetFFCImage()
        {
            bool bStartGrab = false;
            try
            {
                //开采
                m_objDevStream.StartGrab();
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStart").Execute();
                bStartGrab = true;

                //确保得到的是新图
                m_objDevStream.FlushQueue();
                Thread.Sleep(1000);

                IImageData objImgData = m_objDevStream.GetImage(20000);

                // 停采
                m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                m_objDevStream.StopGrab();

                return objImgData;
            }
            catch (CGalaxyException ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<{0}>", ex.Message);
            }
            catch (Exception ex)
            {
                if (bStartGrab)
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("AcquisitionStop").Execute();
                    m_objDevStream.StopGrab();
                }
                Console.WriteLine("<Unknown error>");
            }
            return null;
        }

        /// <summary>
        /// 导出平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：保存成功  false：保存失败</returns>
        public override bool SaveFFC(string strFFCPath)
        {
            if (0 != strFFCPath.Length)
            {
                Console.WriteLine("<Saveing flat-field coefficients, please wait...>");
                return __SaveDeviceFFC(strFFCPath);
            }
            else
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCFlashSave") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCFlashSave"))
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("FFCFlashSave").Execute();
                    return true;
                }
                else
                {
                    return false;
                }
            }

        }

        /// <summary>
        /// 导入平场系数
        /// </summary>
        /// <param name="strFFCPath">平场校正文件路径</param>
        /// <returns>true：加载成功  false：加载失败</returns>
        public override bool LoadFFC(string strFFCPath)
        {
            if (0 != strFFCPath.Length)
            {
                Console.WriteLine("<Loading flat-field coefficients, please wait...>");
                return __LoadDeviceFFC(strFFCPath);
            }
            else
            {
                if (m_objDevRemoteFeatureControl.IsImplemented("FFCFlashLoad") &&
                    m_objDevRemoteFeatureControl.IsWritable("FFCFlashLoad"))
                {
                    m_objDevRemoteFeatureControl.GetCommandFeature("FFCFlashLoad").Execute();
                    return true;
                }
                else
                {
                    return false;
                }
            }
        }
    }
}
