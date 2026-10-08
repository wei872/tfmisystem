#include "StdAfx.h"
#include "GxFlatFieldCorrectionProcess.h"
#include "IGXFactory.h"
#include "GalaxyException.h"
#include <iostream>
#include <fstream>



//--------------------------------------------------
/**
\brief     构造平场矫正处理对象
*/
//--------------------------------------------------
IFlatFieldCorrectionProcess::IFlatFieldCorrectionProcess(CGXStreamPointer pStream, CGXFeatureControlPointer pFeatureControl)
:m_nBlockSize(0)
, m_nFrameCount(0)
, m_nExpectedGray(0)
, m_pFFCCoefficientBuffer(NULL)
, m_nFFCCoefficientSize(0)
, m_pFeatureControl(pFeatureControl)
, m_pStream(pStream)
{
    m_pFlatFieldCorrection = IGXFactory::GetInstance().CreateFlatFieldCorrection();
}

//--------------------------------------------------
/**
\brief     析构平场矫正处理对象
*/
//--------------------------------------------------
IFlatFieldCorrectionProcess::~IFlatFieldCorrectionProcess()
{
    if (NULL != m_pFFCCoefficientBuffer)
    {
        delete m_pFFCCoefficientBuffer;
        m_pFFCCoefficientBuffer = NULL;
    }
}


//--------------------------------------------------
/**
\brief     设置矫正精度
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetBlockSize(int32_t nBlockSize)
{
    try
    {
        if (m_pFeatureControl->IsImplemented("FFCBlockSize") &&
            m_pFeatureControl->IsWritable("FFCBlockSize"))
        {
            m_pFeatureControl->GetEnumFeature("FFCBlockSize")->SetEnumValue(nBlockSize);
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }

    m_nBlockSize = nBlockSize;
}

//--------------------------------------------------
/**
\brief     设置期望灰度值
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetExpectedGray(int32_t nExpectedGray)
{
    try
    {
        bool bSupport = false;
        bSupport = m_pFeatureControl->IsImplemented("FFCExpectedGray");
        if (bSupport)
        {
            m_pFeatureControl->GetIntFeature("FFCExpectedGray")->SetValue(nExpectedGray);
        }
        else
        {
            bSupport = m_pFeatureControl->IsImplemented("FFCExpectGray");
            if (bSupport)
            {
                m_pFeatureControl->GetIntFeature("FFCExpectGray")->SetValue(nExpectedGray);
            }
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }

    m_nExpectedGray = nExpectedGray;
}

//--------------------------------------------------
/**
\brief     设置融合帧数
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetFrameCount(int32_t nFrameCount)
{
    try
    {
        m_pFlatFieldCorrection->SetFrameCount(nFrameCount);

        char chInfo[64] = {"\0"};
        sprintf_s(chInfo, sizeof(chInfo), "FFCFrameCount_%d", nFrameCount);
        if (m_pFeatureControl->IsImplemented("FFCFrameCount") &&
            m_pFeatureControl->IsWritable("FFCFrameCount"))
        {
            m_pFeatureControl->GetEnumFeature("FFCFrameCount")->SetValue(chInfo);
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }
}

//--------------------------------------------------
/**
\brief     判断相机属于那种类型
\return    true支持
*/
//--------------------------------------------------
FFC_TYPE IFlatFieldCorrectionProcess::__GetFFCType(CGXFeatureControlPointer pFeatureControl)
{
	try
	{
		bool bSupport = pFeatureControl->IsImplemented("ShadingCorrectionMode");
		if (!bSupport)
		{
			return FFC_SOFTCAL_SOFTUSE;
		}
		else
		{
			GxIAPICPP::gxstring strSupportType;
			strSupportType = pFeatureControl->GetEnumFeature("ShadingCorrectionMode")->GetValue();

			std::string strType = strSupportType.c_str();
			if ("FlatFieldCorrection" == strType)
			{
				return FFC_SOFTCAL_DEVICEUSE_3140;
			}
			else if ("TailorFlatFieldCorrection" == strType)
			{
				return FFC_SOFTCAL_DEVICEUSE;
			}
			else if ("DeviceFlatFieldCorrection" == strType)
			{
				return FFC_DEVICECAL_DEVICEUSE;
			}
			else
			{
				cout << "< Unknown Device >" << endl;
				return FFC_UNKNOWN;
			}
		}
	}
	catch (CGalaxyException& e)
	{
		cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
		return FFC_UNKNOWN;
	}
	catch (...)
	{	
		cout << "< Unknown error >" << endl;
		return FFC_UNKNOWN;
	}
}



//------------------------------------------------------------------
/**
\brief    设置FFCAccuracy
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetFFCAccuracy(std::string strFFCAccuracy)
{
    try
    {
        if (m_pFeatureControl->IsImplemented("FFCAccuracy") &&
            m_pFeatureControl->IsWritable("FFCAccuracy"))
        {
            m_pFeatureControl->GetEnumFeature("FFCAccuracy")->SetValue(strFFCAccuracy.c_str());
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }
}

//------------------------------------------------------------------
/**
\brief    设置设置期望灰度值使能
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetExpectedGrayEnable(bool bExpectedGrayEnable)
{
    try
    {
		std::string strEnableFFC = bExpectedGrayEnable ? "On" : "Off";
        if (m_pFeatureControl->IsImplemented("FFCExpectedGrayValueEnable") &&
            m_pFeatureControl->IsWritable("FFCExpectedGrayValueEnable"))
        {
            m_pFeatureControl->GetEnumFeature("FFCExpectedGrayValueEnable")->SetValue(strEnableFFC.c_str());
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }
}

//------------------------------------------------------------------
/**
\brief    设置平场校正系数选择
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetCoefficient(std::string strFFCCoefficient)
{
    try
    {
        if (m_pFeatureControl->IsImplemented("FFCCoefficient") &&
            m_pFeatureControl->IsWritable("FFCCoefficient"))
        {
            m_pFeatureControl->GetEnumFeature("FFCCoefficient")->SetValue(strFFCCoefficient.c_str());
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }
}

//------------------------------------------------------------------
/**
\brief    计算平场矫正系数
*/
//-----------------------------------------------------------------
bool IFlatFieldCorrectionProcess::Calculate(bool bNeedDark)
{
    CImageDataPointer pImgData;
    bool bStartGrab = false;
    try
    {
        //开启流层采集
        m_pStream->StartGrab();
        //开启相机采集
        m_pFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();
        bStartGrab = true;

        pImgData = m_pStream->DQBuf(1000);

        GX_FLAT_FIELD_CORRECTION_PARAMETER stParam;
        stParam.pBrightBuf = pImgData->GetBuffer();

		FFC_TYPE emFFCType = __GetFFCType(m_pFeatureControl);
		if (emFFCType == FFC_SOFTCAL_DEVICEUSE)
		{
			stParam.pDarkBuf = NULL; //该类相机不支持暗场直接设置为空
		}
		else 
		{
			if (bNeedDark)
			{
				cout << "Dark field acquisition will start. Please cover the lens and press any key to continue." << endl;
				getchar();

				//确保得到的是新图 s7588
				Sleep(1000);
				stParam.pDarkBuf = m_pStream->GetImage(2000)->GetBuffer();
			}
			else
			{
				stParam.pDarkBuf = NULL;   //  暗场图像可选 传NULL表示不用暗场计算
			}
		}
        
        stParam.emPixelFormat = pImgData->GetPixelFormat();
        stParam.nImgWid = pImgData->GetWidth();
        stParam.nImgHei = pImgData->GetHeight();

        stParam.nFFCBlockSize = m_nBlockSize;
        stParam.nFFCExpectedGray = m_nExpectedGray;

        //获取平场系数大小分配内存
        int32_t pnFFCCoefficientsSize = 0;
        pnFFCCoefficientsSize = m_pFlatFieldCorrection->GetCoefficientsSize(&stParam);

        if (NULL != m_pFFCCoefficientBuffer)
        {
            delete m_pFFCCoefficientBuffer;
            m_pFFCCoefficientBuffer = NULL;
            m_nFFCCoefficientSize = 0;
        }

        m_pFFCCoefficientBuffer = new(std::nothrow) unsigned char[pnFFCCoefficientsSize];
        memset(m_pFFCCoefficientBuffer, 0, pnFFCCoefficientsSize);
        m_nFFCCoefficientSize = pnFFCCoefficientsSize;

        //通过算法接口计算平场系数
        m_pFlatFieldCorrection->Calculate(&stParam, m_pFFCCoefficientBuffer, &pnFFCCoefficientsSize);

        m_pStream->QBuf(pImgData);

        //停采
        m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
        m_pStream->StopGrab();
		return true;
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;

		if (NULL != m_pFFCCoefficientBuffer)
		{
			delete m_pFFCCoefficientBuffer;
			m_pFFCCoefficientBuffer = NULL;
			m_nFFCCoefficientSize = 0;
		}

        if (!pImgData.IsNull())
        {
            m_pStream->QBuf(pImgData);
        }

        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
		return false;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;

		if (NULL != m_pFFCCoefficientBuffer)
		{
			delete m_pFFCCoefficientBuffer;
			m_pFFCCoefficientBuffer = NULL;
			m_nFFCCoefficientSize = 0;
		}

        if (!pImgData.IsNull())
        {
            m_pStream->QBuf(pImgData);
        }

        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
		return false;
    }
}

//------------------------------------------------------------------
/**
\brief    开启平场校正开关
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::EnableFFC(bool bEnableFFC)
{
    try
    {
		std::string strEnableFFC = bEnableFFC ? "On" : "Off";
        if (m_pFeatureControl->IsImplemented("FlatFieldCorrection") &&
            m_pFeatureControl->IsWritable("FlatFieldCorrection"))
        {
            m_pFeatureControl->GetEnumFeature("FlatFieldCorrection")->SetValue(strEnableFFC.c_str());
        }
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
    }
}

//------------------------------------------------------------------
/**
\brief    创建平场对象
*/
//-----------------------------------------------------------------
std::auto_ptr<IFlatFieldCorrectionProcess> IFlatFieldCorrectionProcess::CreateFlatFieldCorrectionProcess(CGXStreamPointer pStream, CGXFeatureControlPointer pFeatureControl)
{
    FFC_TYPE emFFCType = __GetFFCType(pFeatureControl);
    switch(emFFCType)
    {
	case FFC_SOFTCAL_SOFTUSE:
         return std::auto_ptr<IFlatFieldCorrectionProcess>(new CGXSoftCalSoftUseFFC(pStream, pFeatureControl));
         break;
	case FFC_SOFTCAL_DEVICEUSE:
	case FFC_SOFTCAL_DEVICEUSE_3140:
        return std::auto_ptr<IFlatFieldCorrectionProcess>(new CGXSoftCalDeviceUseFFC(pStream, pFeatureControl));
        break;
	case FFC_DEVICECAL_DEVICEUSE:
        return std::auto_ptr<IFlatFieldCorrectionProcess>(new CGXDeviceCalDeviceUseFFC(pStream, pFeatureControl));
        break;
	case FFC_UNKNOWN:
        return std::auto_ptr<IFlatFieldCorrectionProcess>();
        break;
    default:
        break;
    }
    return std::auto_ptr<IFlatFieldCorrectionProcess>();
}

//------------------------------------------------------------------
/**
\brief    导出平场系数
\param    strFFCPath 平场系数ffc文件路径
*/
//-----------------------------------------------------------------
bool IFlatFieldCorrectionProcess::__SavePCFFC(const std::string& strFFCPath)
{
	if (NULL == m_nFFCCoefficientSize)
	{
		return false;
	}

	std::ofstream objFile(strFFCPath.c_str(), std::ios::binary | std::ios::trunc);
	if (!objFile.is_open())
	{
		cout << "< open file " << strFFCPath << " error." << endl;
		return false;
	}

	objFile.write(reinterpret_cast<char*>(m_pFFCCoefficientBuffer), m_nFFCCoefficientSize);

	objFile.close();
    cout << "< save FFC file " << strFFCPath << " successfully." << endl;
	return true;
}

//------------------------------------------------------------------
/**
\brief    导入平场系数
\param    strFFCPath 平场系数ffc文件路径
*/
//-----------------------------------------------------------------
bool IFlatFieldCorrectionProcess::__LoadPCFFC(const std::string& strFFCPath)
{
	std::ifstream objFile(strFFCPath.c_str(), std::ios::binary);
	if (!objFile.is_open())
	{
		cout << "< open file " << strFFCPath << " error." << endl;
		return false;
	}

	//支持再清空
	if (NULL != m_pFFCCoefficientBuffer)
	{
		delete m_pFFCCoefficientBuffer;
		m_pFFCCoefficientBuffer = NULL;
		m_nFFCCoefficientSize = 0;
	}

	//1.获取文件大小
	objFile.seekg(0, std::ios::end);
	m_nFFCCoefficientSize = objFile.tellg();
	objFile.seekg(0, std::ios::beg);

	//2.分配缓存
	m_pFFCCoefficientBuffer = new(std::nothrow) unsigned char[m_nFFCCoefficientSize];
	memset(m_pFFCCoefficientBuffer, 0, m_nFFCCoefficientSize);

	//3.读取平场系数
	objFile.read(reinterpret_cast<char*>(m_pFFCCoefficientBuffer), m_nFFCCoefficientSize);

	objFile.close();
    cout << "< load FFC file " << strFFCPath << " successfully." << endl;
	return true;
}

//导出平场系数
bool IFlatFieldCorrectionProcess::__SaveDeviceFFC(const std::string& strFFCPath)
{
	if (!m_pFeatureControl->IsImplemented("FFCCoefficientsSize") ||
		!m_pFeatureControl->IsReadable("FFCCoefficientsSize") ||
		!m_pFeatureControl->IsImplemented("FFCValueAll") ||
		!m_pFeatureControl->IsWritable("FFCValueAll"))
	{
		return false;
	} 

	int32_t  nFFCCoefficientSize = m_pFeatureControl->GetIntFeature("FFCCoefficientsSize")->GetValue();
	uint8_t* pFFCCoefficientBuffer = new(std::nothrow) uint8_t[nFFCCoefficientSize];
	memset(pFFCCoefficientBuffer, 0, nFFCCoefficientSize);

	m_pFeatureControl->GetRegisterFeature("FFCValueAll")->GetBuffer(pFFCCoefficientBuffer, nFFCCoefficientSize);

	std::ofstream objFile(strFFCPath.c_str(), std::ios::binary | std::ios::trunc);
	if (!objFile.is_open())
	{
		{
			delete[] pFFCCoefficientBuffer;
			pFFCCoefficientBuffer = NULL;
		}
		cout << "< open file " << strFFCPath << " error." << endl;
		return false;
	}

	objFile.write(reinterpret_cast<char*>(pFFCCoefficientBuffer), nFFCCoefficientSize);

	objFile.close();

	{
		delete[] pFFCCoefficientBuffer;
		pFFCCoefficientBuffer = NULL;
	}
    cout << "< save FFC file " << strFFCPath << " successfully." << endl;
	return true;
}

//导入平场系数
bool IFlatFieldCorrectionProcess::__LoadDeviceFFC(const std::string& strFFCPath)
{
	if (!m_pFeatureControl->IsImplemented("FFCCoefficientsSize") ||
		!m_pFeatureControl->IsReadable("FFCCoefficientsSize") ||
		!m_pFeatureControl->IsImplemented("FFCValueAll") ||
		!m_pFeatureControl->IsWritable("FFCValueAll"))
	{
		return false;
	} 

	std::ifstream objFile(strFFCPath.c_str(), std::ios::binary);
	if (!objFile.is_open())
	{
		cout << "< open file " << strFFCPath << " error." << endl;
		return false;
	}

	//1.获取文件大小
	objFile.seekg(0, std::ios::end);
	int32_t  nFFCCoefficientSize = objFile.tellg();
	objFile.seekg(0, std::ios::beg);


	//2.分配缓存
	uint8_t* pFFCCoefficientBuffer = new(std::nothrow) uint8_t[nFFCCoefficientSize];
	memset(pFFCCoefficientBuffer, 0, nFFCCoefficientSize);

	//3.读取平场系数
	objFile.read(reinterpret_cast<char*>(pFFCCoefficientBuffer), nFFCCoefficientSize);

	m_pFeatureControl->GetRegisterFeature("FFCValueAll")->SetBuffer(pFFCCoefficientBuffer, nFFCCoefficientSize);

	objFile.close();

	{
		delete[] pFFCCoefficientBuffer;
		pFFCCoefficientBuffer = NULL;
	}
    cout << "< load FFC file " << strFFCPath << " successfully." << endl;
	return true;
}


CGXSoftCalSoftUseFFC::CGXSoftCalSoftUseFFC(CGXStreamPointer pStream, CGXFeatureControlPointer pFeatureControl)
: IFlatFieldCorrectionProcess(pStream, pFeatureControl)
, m_bEnableFFC(false)
{

}

CGXSoftCalSoftUseFFC::~CGXSoftCalSoftUseFFC()
{

}

//------------------------------------------------------------------
/**
\brief    设置设置平场参数
*/
//-----------------------------------------------------------------
void CGXSoftCalSoftUseFFC::SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam)
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

    //3. 设置融合帧数
    __SetFrameCount(stFFCParam.nFFCFrameCount);
}

//--------------------------------------------------
/**
\brief     对图像应用平场系数，需先计算系数后应用系数
\param     pImageData        [in] 应用平场系数后的图\
return 平场校正后的图像
*/
//--------------------------------------------------
CImageDataPointer CGXSoftCalSoftUseFFC::GetFFCImage()
{
    bool bStartGrab = false;
    try
    {
        //开采
        m_pStream->StartGrab();
        m_pFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();
        bStartGrab = true;


        //确保得到的是新图
        m_pStream->FlushQueue();
        Sleep(1000);

        CImageDataPointer pImageData = m_pStream->GetImage(2000);

		//如果用户启用平场则 应用平场系数
		if (m_bEnableFFC)
		{
			m_pFlatFieldCorrection->FlatFieldCorrection(pImageData->GetBuffer(), pImageData->GetBuffer(),
				GX_ACTUAL_BITS_8, pImageData->GetWidth(), pImageData->GetHeight(), m_pFFCCoefficientBuffer, &m_nFFCCoefficientSize);
		}

        //停采
        m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
        m_pStream->StopGrab();

        return pImageData;
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
    }

    return CImageDataPointer();
}

//------------------------------------------------------------------
/**
\brief    开启平场校正开关
*/
//-----------------------------------------------------------------
void CGXSoftCalSoftUseFFC::EnableFFC(bool bEnableFFC)
{
	m_bEnableFFC = bEnableFFC;
}

//------------------------------------------------------------------
/**
\brief    导出平场系数
\param    strFFCPath 平场系数ffc文件路径
*/
//-----------------------------------------------------------------
bool CGXSoftCalSoftUseFFC::SaveFFC(const std::string& strFFCPath)
{
	return __SavePCFFC(strFFCPath);
}

//------------------------------------------------------------------
/**
\brief    导入平场系数
\param    strFFCPath 平场系数ffc文件路径
*/
//-----------------------------------------------------------------
bool CGXSoftCalSoftUseFFC::LoadFFC(const std::string& strFFCPath)
{
	return __LoadPCFFC(strFFCPath);
}


CGXSoftCalDeviceUseFFC::CGXSoftCalDeviceUseFFC(CGXStreamPointer pStream, CGXFeatureControlPointer pFeatureControl)
: IFlatFieldCorrectionProcess(pStream, pFeatureControl)
{

}

CGXSoftCalDeviceUseFFC::~CGXSoftCalDeviceUseFFC()
{

}

//------------------------------------------------------------------
/**
\brief    设置设置平场参数
*/
//-----------------------------------------------------------------
void CGXSoftCalDeviceUseFFC::SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam)
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

//--------------------------------------------------
/**
\brief     对图像应用平场系数，需先计算系数后应用系数
\param     pImageData        [in] 应用平场系数后的图\
return 平场校正后的图像
*/
//--------------------------------------------------
CImageDataPointer CGXSoftCalDeviceUseFFC::GetFFCImage()
{
    bool bStartGrab = false;
    try
    {
        //若节点不可访问等抛出异常 直接打印错误日志
        m_pFeatureControl->GetRegisterFeature("FFCValueAll")->SetBuffer(m_pFFCCoefficientBuffer, m_nFFCCoefficientSize);

        //开采
        m_pStream->StartGrab();
        m_pFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();
        bStartGrab = true;

        //确保得到的是新图
        m_pStream->FlushQueue();
        Sleep(1000);

        CImageDataPointer pImageData = m_pStream->GetImage(2000);

        //停采
        m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
        m_pStream->StopGrab();

        return pImageData;
    }
    catch (CGalaxyException& e)
    {
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
        cout << "< Unknown error >" << endl;
    }

    return CImageDataPointer();
}

//--------------------------------------------------
/**
\brief     导出平场系数
\param     strFFCPath        [in] 导出路径，如果为空则导出到相机
return  成功true，失败false
*/
//--------------------------------------------------
bool CGXSoftCalDeviceUseFFC::SaveFFC(const std::string& strFFCPath)
{
	if (!strFFCPath.empty())
	{
		return __SaveDeviceFFC(strFFCPath);
	}
	else
	{
		if (m_pFeatureControl->IsImplemented("FFCFlashSave") &&
			m_pFeatureControl->IsWritable("FFCFlashSave"))
		{
			m_pFeatureControl->GetCommandFeature("FFCFlashSave")->Execute();
			return true;
		} 
		else
			return false;
	}
}

//--------------------------------------------------
/**
\brief     导入平场系数
\param     strFFCPath        [in] 导入路径，如果为空则导出到相机
return  成功true，失败false
*/
//--------------------------------------------------
bool CGXSoftCalDeviceUseFFC::LoadFFC(const std::string& strFFCPath)
{
	if (!strFFCPath.empty())
	{
		return __LoadDeviceFFC(strFFCPath);
	}
	else
	{
		if (!m_pFeatureControl->IsImplemented("FFCCoefficientsSize") ||
			!m_pFeatureControl->IsReadable("FFCCoefficientsSize") ||
			!m_pFeatureControl->IsImplemented("FFCFlashLoad") ||
			!m_pFeatureControl->IsWritable("FFCFlashLoad") ||
			!m_pFeatureControl->IsImplemented("FFCValueAll") ||
			!m_pFeatureControl->IsWritable("FFCValueAll"))
		{
			return false;
		} 

		//支持再清空旧数据， 防止抛异常报错吧旧系数也没了
		if (NULL != m_pFFCCoefficientBuffer)
		{
			delete m_pFFCCoefficientBuffer;
			m_pFFCCoefficientBuffer = NULL;
			m_nFFCCoefficientSize = 0;
		}

		m_nFFCCoefficientSize = m_pFeatureControl->GetIntFeature("FFCCoefficientsSize")->GetValue();
		m_pFFCCoefficientBuffer = new(std::nothrow) unsigned char[m_nFFCCoefficientSize];
		memset(m_pFFCCoefficientBuffer, 0, m_nFFCCoefficientSize);

		m_pFeatureControl->GetCommandFeature("FFCFlashLoad")->Execute();
		m_pFeatureControl->GetRegisterFeature("FFCValueAll")->GetBuffer(m_pFFCCoefficientBuffer, m_nFFCCoefficientSize);
		return true;
	}
}

CGXDeviceCalDeviceUseFFC::CGXDeviceCalDeviceUseFFC(CGXStreamPointer pStream, CGXFeatureControlPointer pFeatureControl)
: IFlatFieldCorrectionProcess(pStream, pFeatureControl)
{

}

CGXDeviceCalDeviceUseFFC::~CGXDeviceCalDeviceUseFFC()
{

}

void CGXDeviceCalDeviceUseFFC::SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam)
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

//计算系数
bool CGXDeviceCalDeviceUseFFC::Calculate(bool bNeedDark)
{
    bool bStartGrab = false;
	bool bCalculate = false;
    try
    {
        //开启流层采集
        m_pStream->StartGrab();
        //开启相机采集
        m_pFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();
        bStartGrab = true;

        if (m_pFeatureControl->IsImplemented("FFCGenerate") &&
            m_pFeatureControl->IsWritable("FFCGenerate"))
        {
            m_pFeatureControl->GetCommandFeature("FFCGenerate")->Execute();
			bCalculate = true;
        } 

        //停采
        m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
        m_pStream->StopGrab();
    }
    catch (CGalaxyException& e)
    {
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
		bCalculate = false;
    }
    catch (...)
    {	
        cout << "< Unknown error >" << endl;

        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
		bCalculate = false;
    }

	return bCalculate;
}

//获取平场校正后的图像
CImageDataPointer CGXDeviceCalDeviceUseFFC::GetFFCImage()
{
    bool bStartGrab = false;
    try
    {
        //开采
        m_pStream->StartGrab();
        m_pFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();
        bStartGrab = true;

        //确保得到的是新图
        m_pStream->FlushQueue();
        Sleep(1000);

        CImageDataPointer pImageData = m_pStream->GetImage(2000);

        //停采
        m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
        m_pStream->StopGrab();

        return pImageData;
    }
    catch (CGalaxyException& e)
    {
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
        cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
    }
    catch (...)
    {	
        if (bStartGrab)
        {
            //停采
            m_pFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
            m_pStream->StopGrab();
        }
        cout << "< Unknown error >" << endl;
    }

    return CImageDataPointer();
}

//--------------------------------------------------
/**
\brief     导出平场系数
\param     strFFCPath        [in] 导出路径，如果为空则导出到相机
return  成功true，失败false
*/
//--------------------------------------------------
bool CGXDeviceCalDeviceUseFFC::SaveFFC(const std::string& strFFCPath)
{
	if (!strFFCPath.empty())
	{
		cout << "<Saveing flat-field coefficients, please wait...>" << endl;
		return __SaveDeviceFFC(strFFCPath);
	}
	else
	{
		if (m_pFeatureControl->IsImplemented("FFCFlashSave") &&
			m_pFeatureControl->IsWritable("FFCFlashSave"))
		{
			m_pFeatureControl->GetCommandFeature("FFCFlashSave")->Execute();
			return true;
		} 
		else
			return false;
	}
}

//--------------------------------------------------
/**
\brief     导入平场系数
\param     strFFCPath        [in] 导入路径，如果为空则导出到相机
return  成功true，失败false
*/
//--------------------------------------------------
bool CGXDeviceCalDeviceUseFFC::LoadFFC(const std::string& strFFCPath)
{
	if (!strFFCPath.empty())
	{
		cout << "<Loading flat-field coefficients, please wait...>" << endl;
		return __LoadDeviceFFC(strFFCPath);
	}
	else
	{
		if (m_pFeatureControl->IsImplemented("FFCFlashLoad") &&
			m_pFeatureControl->IsWritable("FFCFlashLoad"))
		{
            m_pFeatureControl->GetCommandFeature("FFCFlashLoad")->Execute();
			return true;
		} 
        else
        {
            return false;
        }
	}
}