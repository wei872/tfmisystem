#include "GxFlatFieldCorrectionProcess.h"
#include <stdio.h>
#include <cstdlib> 
#include <iostream>
#include <sstream>
#include <fstream>
//----------------------------------------------------------------------------------
/**
\brief  获取错误信息描述
\param  emErrorStatus  错误码

\return void
*/
//----------------------------------------------------------------------------------
void __GetErrorString(GX_STATUS emErrorStatus)
{
	char      *pchErrorInfo = NULL;
	size_t    nSize         = 0;
	GX_STATUS emStatus      = GX_STATUS_SUCCESS;
	
	// 获取错误描述信息长度
	emStatus = GXGetLastError(&emErrorStatus, NULL, &nSize);
	pchErrorInfo = new(std::nothrow) char[nSize];
	if (pchErrorInfo == NULL)
	{
		printf("<Failed to allocate memory>\n");
		return ;
	}
	
	// 获取错误信息描述
	emStatus = GXGetLastError(&emErrorStatus, pchErrorInfo, &nSize);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		printf("<GXGetLastError接口调用失败!>\n");
	}

	printf("%s\n", (LPCTSTR)pchErrorInfo);

	// 释放资源
	if (pchErrorInfo != NULL)
	{
		delete[]pchErrorInfo;
		pchErrorInfo = NULL;
	}
}

//--------------------------------------------------
/**
\brief     构造平场矫正处理对象
*/
//--------------------------------------------------
IFlatFieldCorrectionProcess::IFlatFieldCorrectionProcess(GX_DEV_HANDLE  hDevice)
	:m_nBlockSize(0)
	, m_nFrameCount(0)
	, m_nExpectedGray(0)
	, m_pFFCCoefficientBuffer(NULL)
	, m_nFFCCoefficientSize(0)
	, m_hDevice(hDevice)
	, m_hFlatFieldCorrection(NULL)
{
	DxFFCCreate(&m_hFlatFieldCorrection);
	m_stFrameData.pImgBuf = NULL;
}

//--------------------------------------------------
/**
\brief     析构平场矫正处理对象
*/
//--------------------------------------------------
IFlatFieldCorrectionProcess::~IFlatFieldCorrectionProcess()
{
	//释放图像缓冲区buffer
	if(NULL != m_stFrameData.pImgBuf)
	{
		free(m_stFrameData.pImgBuf);	
		m_stFrameData.pImgBuf = NULL;
	}

	if (NULL != m_hFlatFieldCorrection)
	{
		DxFFCDestroy(m_hFlatFieldCorrection);
		m_hFlatFieldCorrection = NULL;
	}

	if (NULL != m_pFFCCoefficientBuffer)
	{
		delete m_pFFCCoefficientBuffer;
		m_pFFCCoefficientBuffer = NULL;
		m_nFFCCoefficientSize = 0;
	}

}


//--------------------------------------------------
/**
\brief     设置矫正精度
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetBlockSize(int32_t i32BlockSize)
{
	GX_STATUS emStatus  = GX_STATUS_SUCCESS;
	bool      bSupport = false;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCBlockSize", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			std::ostringstream oss;
			oss << i32BlockSize; 
			emStatus = GXSetEnumValueByString(m_hDevice, "FFCBlockSize",oss.str().c_str());	
			GX_VERIFY_BREAK(emStatus);
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	}
	
	m_nBlockSize = i32BlockSize;
	return;
}

//--------------------------------------------------
/**
\brief     设置期望灰度值
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetExpectedGray(int32_t nExpectedGray)
{
	GX_STATUS emStatus  = GX_STATUS_SUCCESS;
	bool      bSupport = false;

	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCExpectedGray", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetIntValue(m_hDevice, "FFCExpectedGray", nExpectedGray);
			GX_VERIFY_BREAK(emStatus);
		}
		else
		{
			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCExpectGray", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			if (bSupport)
			{
				emStatus = GXSetIntValue(m_hDevice, "FFCExpectGray", nExpectedGray);
				GX_VERIFY_BREAK(emStatus);
			}
		}

		m_nExpectedGray = nExpectedGray;

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	}

	return;
}

//--------------------------------------------------
/**
\brief     设置融合帧数
\return    设置失败返回false
*/
//--------------------------------------------------
void IFlatFieldCorrectionProcess::__SetFrameCount(int32_t i32FrameCount)
{
	GX_STATUS  emStatus  = GX_STATUS_SUCCESS;
	VxInt32   emDxStatus = DX_OK;
	bool      bSupport   = false;
	do
	{
		char chInfo[64] = {"\0"};
		sprintf_s(chInfo, sizeof(chInfo), "FFCFrameCount_%d", i32FrameCount);
		
		emDxStatus = DxFFCSetFrameCount(m_hFlatFieldCorrection,i32FrameCount);
		DX_VERIFY(emDxStatus);

		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCFrameCount", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetEnumValueByString(m_hDevice, "FFCFrameCount",chInfo);	
			GX_VERIFY_BREAK(emStatus);
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	}

	return;
}

//--------------------------------------------------
/**
\brief     判断相机属于那种类型
\return    true支持
*/
//--------------------------------------------------
FFC_TYPE IFlatFieldCorrectionProcess::__GetFFCType(GX_DEV_HANDLE hDevice)
{
	bool bSupport = false;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(hDevice, "ShadingCorrectionMode", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		if (!bSupport)
		{
			return FFC_SOFTCAL_SOFTUSE;
		}
		else
		{
			GX_ENUM_VALUE  stValue;
			emStatus = GXGetEnumValue(hDevice, "ShadingCorrectionMode",&stValue);

			if (0 == strcmp("FlatFieldCorrection",stValue.stCurValue.strCurSymbolic))
			{
				return FFC_SOFTCAL_DEVICEUSE_3140;
			}
			else if (0 == strcmp("TailorFlatFieldCorrection",stValue.stCurValue.strCurSymbolic))
			{
				return FFC_SOFTCAL_DEVICEUSE;
			}
			else if (0 == strcmp("DeviceFlatFieldCorrection",stValue.stCurValue.strCurSymbolic))
			{
				return FFC_DEVICECAL_DEVICEUSE;
			}
			else
			{
				cout << "< Unknown Device >" << endl;
				return FFC_UNKNOWN;
			}
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	    return FFC_UNKNOWN;
	}

	return FFC_UNKNOWN;
}

//------------------------------------------------------------------
/**
\brief    设置FFCAccuracy
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetFFCAccuracy(std::string strFFCAccuracy)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool      bSupport = false;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCAccuracy", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetEnumValueByString(m_hDevice, "FFCAccuracy",strFFCAccuracy.c_str());	
			GX_VERIFY_BREAK(emStatus);
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);	   
	}

	return ;
}

//------------------------------------------------------------------
/**
\brief    设置期望灰度值使能
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetExpectedGrayEnable(bool bExpectedGrayEnable)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool      bSupport = false;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;

		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCExpectedGrayValueEnable", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{    
			std::string strEnableFFC = bExpectedGrayEnable ? "On" : "Off";
			emStatus = GXSetEnumValueByString(m_hDevice, "FFCExpectedGrayValueEnable",strEnableFFC.c_str());	
			GX_VERIFY_BREAK(emStatus);	
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);  
	}

	return;
}

//------------------------------------------------------------------
/**
\brief    设置平场校正系数选择
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::__SetCoefficient(std::string strFFCCoefficient)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupport = false;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;

		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCCoefficient", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{    
			emStatus = GXSetEnumValueByString(m_hDevice, "FFCCoefficient",strFFCCoefficient.c_str());	
			GX_VERIFY_BREAK(emStatus);	
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	}

	return;
}

//------------------------------------------------------------------
/**
\brief    计算平场矫正系数
*/
//-----------------------------------------------------------------
bool IFlatFieldCorrectionProcess::Calculate(bool bNeedDark)
{
	GX_STATUS emStatus    = GX_STATUS_ERROR;
	VxInt32   emDxStatus  = DX_OK;
	bool      bStartGrab  = false;
	PGX_FRAME_BUFFER  pFrameBuffer = new(std::nothrow)GX_FRAME_BUFFER();
	if(NULL == pFrameBuffer)
	{
		return false;
	}

	do
	{
		//开启相机采集
		emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStart");
		if (GX_STATUS_SUCCESS == emStatus)
		{
			bStartGrab = true;

			//确保得到的是新图
			emStatus = GXFlushQueue(m_hDevice);
			GX_VERIFY_BREAK(emStatus);

			Sleep(1000);

			///调用GXDQBuf取一帧图像
			emStatus = GXDQBuf(m_hDevice,&pFrameBuffer, 5000);
			GX_VERIFY_BREAK(emStatus);
		}

		FLAT_FIELD_CORRECTION_PARAMETER stParam;
		stParam.pBrightBuf = (void*)pFrameBuffer->pImgBuf;
		FFC_TYPE emFFCType = __GetFFCType(m_hDevice);

		if (emFFCType == FFC_SOFTCAL_DEVICEUSE)
		{
			stParam.pDarkBuf = NULL; //该类相机不支持暗场直接设置为空
		}
		else
		{
			if (bNeedDark)
			{
				printf("Dark field acquisition will start. Please cover the lens and press any key to continue.\n");
				getchar();

				//确保得到的是新图
				Sleep(1000);

				GX_INT_VALUE stPayLoadSize;

				//获取图像buffer大小，下面动态申请内存
				emStatus = GXGetIntValue(m_hDevice, "PayloadSize", &stPayLoadSize);
				if(GX_STATUS_SUCCESS == emStatus && stPayLoadSize.nCurValue > 0)
				{
					if(NULL != m_stFrameData.pImgBuf)
					{
						free(m_stFrameData.pImgBuf);
						m_stFrameData.pImgBuf = NULL;
					}

					//根据获取的图像buffer大小m_nPayLoadSize申请buffer
					m_stFrameData.pImgBuf= malloc((size_t)stPayLoadSize.nCurValue);
					if(NULL == m_stFrameData.pImgBuf)
					{
						break;
					}

					Sleep(1000);

					//调用GXGetImage取一帧图像
					while(GXGetImage(m_hDevice, &m_stFrameData, 2000) != GX_STATUS_SUCCESS)
					{
						Sleep(10);	
					}
					if (m_stFrameData.nStatus == GX_FRAME_STATUS_SUCCESS)
					{
						stParam.pDarkBuf = m_stFrameData.pImgBuf;
					}
				}		
			}
			else
			{
				stParam.pDarkBuf = NULL;   //  暗场图像可选 传NULL表示不用暗场计算
			}
		}

		stParam.emPixelFormat   = (GX_PIXEL_FORMAT_ENTRY)pFrameBuffer->nPixelFormat;
		stParam.nImgWid         = pFrameBuffer->nWidth;
		stParam.nImgHei         = pFrameBuffer->nHeight;
		stParam.nFFCBlockSize    = m_nBlockSize;
		stParam.nFFCExpectedGray = m_nExpectedGray;

		//获取平场系数大小分配内存
		int32_t pnFFCCoefficientsSize = 0;
		emDxStatus = DxFFCGetCoefficientsSize(m_hFlatFieldCorrection, &stParam, &pnFFCCoefficientsSize);
		DX_VERIFY(emDxStatus);

		if (NULL != m_pFFCCoefficientBuffer)
		{
			delete m_pFFCCoefficientBuffer;
			m_pFFCCoefficientBuffer = NULL;
			m_nFFCCoefficientSize = 0;
		}

		m_pFFCCoefficientBuffer = new(std::nothrow) unsigned char[pnFFCCoefficientsSize];
		if (NULL == m_pFFCCoefficientBuffer)
		{
			break;
		}

		memset(m_pFFCCoefficientBuffer, 0, pnFFCCoefficientsSize);
		m_nFFCCoefficientSize = pnFFCCoefficientsSize;

		//通过算法接口计算平场系数
		emDxStatus = DxFFCCalculate(m_hFlatFieldCorrection, &stParam, m_pFFCCoefficientBuffer, &pnFFCCoefficientsSize);
		DX_VERIFY(emDxStatus);

		emStatus = GXQBuf(m_hDevice, pFrameBuffer);
		GX_VERIFY_BREAK(emStatus);

		emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");
		GX_VERIFY_BREAK(emStatus);

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if (NULL != m_pFFCCoefficientBuffer)
		{
			delete m_pFFCCoefficientBuffer;
			m_pFFCCoefficientBuffer = NULL;
			m_nFFCCoefficientSize = 0;
		}

		if (NULL != pFrameBuffer)
        {
            emStatus = GXQBuf(m_hDevice, pFrameBuffer);
			pFrameBuffer = NULL;
        }

		if(bStartGrab)
		{
			emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");
		}

		return false;
	}

	return true;
}

//------------------------------------------------------------------
/**
\brief    开启平场校正开关
*/
//-----------------------------------------------------------------
void IFlatFieldCorrectionProcess::EnableFFC(bool bEnableFFC)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool      bSupport = false;

	do
	{
		std::string strEnableFFC = bEnableFFC ? "On" : "Off";
		GX_NODE_ACCESS_MODE emAccessMode;

		emStatus = GXGetNodeAccessMode(m_hDevice, "FlatFieldCorrection", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetEnumValueByString(m_hDevice, "FlatFieldCorrection",strEnableFFC.c_str());	
			GX_VERIFY_BREAK(emStatus);
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);
	}

	return;

}

//------------------------------------------------------------------
/**
\brief    创建平场对象
*/
//-----------------------------------------------------------------
IFlatFieldCorrectionProcess* IFlatFieldCorrectionProcess::CreateFlatFieldCorrectionProcess(const GX_DEV_HANDLE hDevice)
{
	// 获取平场类型
	FFC_TYPE emFFCType = __GetFFCType(hDevice);

	switch(emFFCType)
	{
	case FFC_SOFTCAL_SOFTUSE:
		return new CGXSoftCalSoftUseFFC(hDevice);
		break;
	case FFC_SOFTCAL_DEVICEUSE:
		break;
	case FFC_SOFTCAL_DEVICEUSE_3140:
		return new CGXSoftCalDeviceUseFFC(hDevice);
		break;
	case FFC_DEVICECAL_DEVICEUSE:
		return new CGXDeviceCalDeviceUseFFC(hDevice);
		break;
	case FFC_UNKNOWN:
		return NULL;
		break;
	default:
		break;
	}

	return NULL;
}

//------------------------------------------------------------------
/**
\brief    导出平场系数
\param    strFFCPath 平场系数ffc文件路径
*/
//-----------------------------------------------------------------
bool IFlatFieldCorrectionProcess::__SavePCFFC(const std::string& strFFCPath)
{
	if (0 == m_nFFCCoefficientSize)
	{
		printf("< save file %s Error, FFCCoefficientSize is 0 ",strFFCPath.c_str());
		return false;
	}

	std::ofstream objFile(strFFCPath.c_str(), std::ios::binary | std::ios::trunc);
	if (!objFile.is_open())
	{
		printf("< open file %s error." ,strFFCPath.c_str());
		return false;
	}

	objFile.write(reinterpret_cast<char*>(m_pFFCCoefficientBuffer), m_nFFCCoefficientSize);

	objFile.close();

	printf("<Save FFC parameters to '%s' file successfully.>", strFFCPath.c_str());
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
		printf("< open file %s Error",strFFCPath.c_str());
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
	printf("<Successfully loaded FFC configuration file %s.>", strFFCPath.c_str());
	return true;
}

//导出平场系数
bool IFlatFieldCorrectionProcess::__SaveDeviceFFC(const std::string& strFFCPath)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupportFFCCoefficientsSize = false;
	bool bSupportFFCValueAll        = false;
	uint8_t* pFFCCoefficientBuffer   = NULL;
	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCCoefficientsSize", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupportFFCCoefficientsSize = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCValueAll", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupportFFCValueAll = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		if (!bSupportFFCCoefficientsSize 
			|| !bSupportFFCValueAll)
		{
			printf("<The device does not support loading FFC parameters to device.>");
			return false;
		} 

		GX_INT_VALUE stValue;
		emStatus = GXGetIntValue(m_hDevice,"FFCCoefficientsSize",&stValue);
		GX_VERIFY_BREAK(emStatus);
		size_t  nFFCCoefficientSize = stValue.nCurValue;

		pFFCCoefficientBuffer = new(std::nothrow) uint8_t[nFFCCoefficientSize];
		memset(pFFCCoefficientBuffer, 0, nFFCCoefficientSize);

		emStatus = GXGetRegisterValue(m_hDevice,"FFCValueAll",pFFCCoefficientBuffer,&nFFCCoefficientSize);
		GX_VERIFY_BREAK(emStatus);

		std::ofstream objFile(strFFCPath.c_str(), std::ios::binary | std::ios::trunc);
		if (!objFile.is_open())
		{
			if(NULL != pFFCCoefficientBuffer)
			{
				delete[] pFFCCoefficientBuffer;
				pFFCCoefficientBuffer = NULL;
			}
			printf("< open file &s\n Error", strFFCPath.c_str());
			return false;
		}

		objFile.write(reinterpret_cast<char*>(pFFCCoefficientBuffer), nFFCCoefficientSize);

		objFile.close();

		if(NULL != pFFCCoefficientBuffer)
		{
			delete[] pFFCCoefficientBuffer;
			pFFCCoefficientBuffer = NULL;
		}

		printf("<Save FFC parameters to '%s' file successfully.>", strFFCPath.c_str());

		return true;
	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(NULL != pFFCCoefficientBuffer)
		{
			delete[] pFFCCoefficientBuffer;
			pFFCCoefficientBuffer = NULL;
		}
	}

	return false;	
}

//导入平场系数
bool IFlatFieldCorrectionProcess::__LoadDeviceFFC(const std::string& strFFCPath)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupportFFCCoefficientsSize = false;
	bool bSupportFFCValueAll        = false;
	uint8_t* pFFCCoefficientBuffer   = NULL;

	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCCoefficientsSize", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupportFFCCoefficientsSize = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCValueAll", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupportFFCValueAll = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (!bSupportFFCCoefficientsSize 
			|| !bSupportFFCValueAll)
		{
			return false;
		} 

		std::ifstream objFile(strFFCPath.c_str(), std::ios::binary);
		if (!objFile.is_open())
		{
			printf("< open file &s\n Error", strFFCPath.c_str());
			return false;
		}

		//1.获取文件大小
		objFile.seekg(0, std::ios::end);
		int32_t  nFFCCoefficientSize = objFile.tellg();
		objFile.seekg(0, std::ios::beg);

		//2.分配缓存
		pFFCCoefficientBuffer = new(std::nothrow) uint8_t[nFFCCoefficientSize];
		if (NULL == pFFCCoefficientBuffer)
		{
			break;
		}
		memset(pFFCCoefficientBuffer, 0, nFFCCoefficientSize);

		//3.读取平场系数
		objFile.read(reinterpret_cast<char*>(pFFCCoefficientBuffer), nFFCCoefficientSize);

		emStatus = GXSetRegisterValue(m_hDevice, "FFCValueAll",
				pFFCCoefficientBuffer, nFFCCoefficientSize);
		GX_VERIFY_BREAK(emStatus);
		
		objFile.close();

		if(NULL != pFFCCoefficientBuffer)
		{
			delete pFFCCoefficientBuffer;
			pFFCCoefficientBuffer = NULL;
		}
		printf("<Successfully loaded FFC configuration file %s.>", strFFCPath.c_str());
		return true;

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(NULL != pFFCCoefficientBuffer)
		{
			delete pFFCCoefficientBuffer;
			pFFCCoefficientBuffer = NULL;
		}
	}

	return false;	
}


CGXSoftCalSoftUseFFC::CGXSoftCalSoftUseFFC(const GX_DEV_HANDLE hDevice)
	: IFlatFieldCorrectionProcess(hDevice)
	, m_bEnableFFC(false)
{

}

CGXSoftCalSoftUseFFC::~CGXSoftCalSoftUseFFC()
{

}

//------------------------------------------------------------------
/**
\brief    设置平场参数
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
GX_FRAME_DATA CGXSoftCalSoftUseFFC::GetFFCImage()
{
	GX_STATUS emStatus   = GX_STATUS_ERROR;	
	VxInt32   emDxStatus = DX_OK;
	bool      bStartGrab = false;
	
	do
	{
		//开启相机采集
		GX_INT_VALUE stPayLoadSize;

		//获取图像buffer大小，下面动态申请内存
		emStatus = GXGetIntValue(m_hDevice, "PayloadSize", &stPayLoadSize);
		if(GX_STATUS_SUCCESS == emStatus && stPayLoadSize.nCurValue > 0)
		{
			if(NULL != m_stFrameData.pImgBuf)
			{
				free(m_stFrameData.pImgBuf);
				m_stFrameData.pImgBuf = NULL;
			}

			//根据获取的图像buffer大小m_nPayLoadSize申请buffer
			m_stFrameData.pImgBuf= malloc((size_t)stPayLoadSize.nCurValue);
			if(NULL == m_stFrameData.pImgBuf)
			{
				break;
			}

			//发送开始采集命令
			emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStart");		
			if (GX_STATUS_SUCCESS == emStatus)
			{
				bStartGrab = true;

				//确保得到的是新图
				emStatus = GXFlushQueue(m_hDevice);
				GX_VERIFY_BREAK(emStatus);

				Sleep(1000);

				//调用GXGetImage取一帧图像
				while(GXGetImage(m_hDevice, &m_stFrameData, 2000) != GX_STATUS_SUCCESS)
				{
					Sleep(10);	
				}
				if (m_stFrameData.nStatus == GX_FRAME_STATUS_SUCCESS)
				{
					//如果用户启用平场则 应用平场系数
					if(m_bEnableFFC)
					{
						emDxStatus = DxFlatFieldCorrection(m_stFrameData.pImgBuf, m_stFrameData.pImgBuf,
							DX_ACTUAL_BITS_8, m_stFrameData.nWidth, m_stFrameData.nHeight, m_pFFCCoefficientBuffer, &m_nFFCCoefficientSize);
						DX_VERIFY(emDxStatus);
					}
				}

				//发送停止采集命令
				emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");
				GX_VERIFY_BREAK(emStatus);
			}
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(bStartGrab)
		{
			//发送停止采集命令
			emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");
		}
	}

	return m_stFrameData;
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

//第二类相机，相机本身不能计算平场系数需要依靠软件计算
CGXSoftCalDeviceUseFFC::CGXSoftCalDeviceUseFFC(const GX_DEV_HANDLE hDevice)
	: IFlatFieldCorrectionProcess(hDevice)
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
GX_FRAME_DATA CGXSoftCalDeviceUseFFC::GetFFCImage()
{
	GX_STATUS emStatus     = GX_STATUS_SUCCESS;
	bool      bSupport     = false;
	bool      bStartGrab   = false;

	do
	{
		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCValueAll", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetRegisterValue(m_hDevice, "FFCValueAll",
				m_pFFCCoefficientBuffer, m_nFFCCoefficientSize);
			GX_VERIFY_BREAK(emStatus);
		}

		GX_INT_VALUE stPayLoadSize;

		//获取图像buffer大小，下面动态申请内存
		emStatus = GXGetIntValue(m_hDevice, "PayloadSize", &stPayLoadSize);
		if(GX_STATUS_SUCCESS == emStatus && stPayLoadSize.nCurValue > 0)
		{
			if(NULL != m_stFrameData.pImgBuf)
			{
				free(m_stFrameData.pImgBuf);
				m_stFrameData.pImgBuf = NULL;
			}

			//根据获取的图像buffer大小m_nPayLoadSize申请buffer
			m_stFrameData.pImgBuf= malloc((size_t)stPayLoadSize.nCurValue);
			if(NULL == m_stFrameData.pImgBuf)
			{
				break;
			}

			//发送开始采集命令
			emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStart");		
			if (GX_STATUS_SUCCESS == emStatus)
			{
				bStartGrab = true;

				//确保得到的是新图
				emStatus = GXFlushQueue(m_hDevice);
				GX_VERIFY_BREAK(emStatus);

				Sleep(1000);

				//调用GXGetImage取一帧图像
				while(GXGetImage(m_hDevice, &m_stFrameData, 2000) != GX_STATUS_SUCCESS)
				{
					Sleep(10);	
				}

				//发送停止采集命令
				emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");		
				GX_VERIFY_BREAK(emStatus);
			}

		}
	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(bStartGrab)
		{
			//发送停止采集命令
			emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStop");		
		}
	}

	return m_stFrameData;
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
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupport = false;

	do
	{
		if (!strFFCPath.empty())
		{
			return __SaveDeviceFFC(strFFCPath);
		}
		else
		{
			GX_NODE_ACCESS_MODE emAccessMode;
			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCFlashSave", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			if (!bSupport)
			{
				return false;
			} 

			emStatus = GXSetCommandValue(m_hDevice, "FFCFlashSave");
			GX_VERIFY_BREAK(emStatus);

			return true;
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		return false;
	}

	return true;
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
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupportFFCCoefficientsSize = false;
	bool bSupportFFCFlashLoad       = false;
	bool bSupportFFCValueAll        = false;

	do
	{
		if (!strFFCPath.empty())
		{
			return __LoadDeviceFFC(strFFCPath);
		}
		else
		{
			GX_NODE_ACCESS_MODE emAccessMode;
			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCCoefficientsSize", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupportFFCCoefficientsSize = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_RO))
				? true : false;

			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCFlashLoad", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupportFFCFlashLoad = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCValueAll", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupportFFCValueAll = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			if (!bSupportFFCCoefficientsSize 
				|| !bSupportFFCFlashLoad 
				|| !bSupportFFCValueAll)
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

			GX_INT_VALUE stValue;
			emStatus = GXGetIntValue(m_hDevice,"FFCCoefficientsSize",&stValue);
            GX_VERIFY_BREAK(emStatus);
			m_nFFCCoefficientSize = stValue.nCurValue;

			m_pFFCCoefficientBuffer = new(std::nothrow) unsigned char[m_nFFCCoefficientSize];
			if (NULL == m_pFFCCoefficientBuffer)
			{
				break;
			}
			memset(m_pFFCCoefficientBuffer, 0, m_nFFCCoefficientSize);

			emStatus = GXSetCommandValue(m_hDevice, "FFCFlashLoad");
			GX_VERIFY_BREAK(emStatus);

			size_t sSize= 0;
			emStatus = GXGetRegisterValue(m_hDevice, "FFCValueAll",m_pFFCCoefficientBuffer, &sSize);
			GX_VERIFY_BREAK(emStatus);
			m_nFFCCoefficientSize = sSize;
			return true;
		}
	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		return false;
	}
	return true;
}

CGXDeviceCalDeviceUseFFC::CGXDeviceCalDeviceUseFFC(const GX_DEV_HANDLE hDevice)
	: IFlatFieldCorrectionProcess(hDevice)
{

}

CGXDeviceCalDeviceUseFFC::~CGXDeviceCalDeviceUseFFC()
{

}

//--------------------------------------------------
/**
\brief     导入平场系数
\param     strFFCPath        [in] 导入路径，如果为空则导出到相机
return  成功true，失败false
*/
//--------------------------------------------------
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

//--------------------------------------------------
/**
\brief     计算系数
\param     bNeedDark        [in] 是否需要暗场
return    成功true，失败false
*/
//--------------------------------------------------
bool CGXDeviceCalDeviceUseFFC::Calculate(bool bNeedDark)
{
	GX_STATUS emStatus  = GX_STATUS_SUCCESS;
	bool     bSupport   = false;
	bool     bStartGrab = false;
	bool     bCalculate = false;

	do
	{
		//开启相机采集
		emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStart");
		GX_VERIFY_BREAK(emStatus);

		bStartGrab = true;

		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(m_hDevice, "FFCGenerate", 
			&emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
			|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXSetCommandValue(m_hDevice, "FFCGenerate");
			GX_VERIFY_BREAK(emStatus);
			bCalculate = true;
		} 

		//停采
		emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStop");
		GX_VERIFY_BREAK(emStatus);

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(bStartGrab)
		{
			GXSetCommandValue(m_hDevice, "AcquisitionStop");
		}	
		bCalculate = false;
	}

	return bCalculate;
}

//--------------------------------------------------
/**
\brief     获取平场校正后的图像
return     m_stFrameData   图像结构体
*/
//--------------------------------------------------
GX_FRAME_DATA CGXDeviceCalDeviceUseFFC::GetFFCImage()
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bStartGrab = false;

	do
	{
		GX_INT_VALUE stPayLoadSize;

		//获取图像buffer大小，下面动态申请内存
		emStatus = GXGetIntValue(m_hDevice, "PayloadSize", &stPayLoadSize);
		if(GX_STATUS_SUCCESS == emStatus && stPayLoadSize.nCurValue > 0)
		{
			if(NULL != m_stFrameData.pImgBuf)
			{
				free(m_stFrameData.pImgBuf);
				m_stFrameData.pImgBuf = NULL;
			}

			//根据获取的图像buffer大小m_nPayLoadSize申请buffer
			m_stFrameData.pImgBuf = malloc((size_t)stPayLoadSize.nCurValue);
			if(NULL == m_stFrameData.pImgBuf)
			{
				break;
			}

			//发送开始采集命令
			emStatus = GXSetCommandValue(m_hDevice, "AcquisitionStart");		
			if (GX_STATUS_SUCCESS == emStatus)
			{
				bStartGrab = true;

				//确保得到的是新图
				emStatus = GXFlushQueue(m_hDevice);
				GX_VERIFY_BREAK(emStatus);

				Sleep(1000);

				//调用GXGetImage取一帧图像
				while(GXGetImage(m_hDevice, &m_stFrameData, 2000) != GX_STATUS_SUCCESS)
				{
					Sleep(10);	
				}

				//发送停止采集命令
				emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");		
				GX_VERIFY_BREAK(emStatus);
			}
		}

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		if(bStartGrab)
		{
			//发送停止采集命令
			emStatus = GXSetCommandValue (m_hDevice, "AcquisitionStop");		
		}
	}

	return m_stFrameData;
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
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupport = false;

	do
	{
		if (!strFFCPath.empty())
		{
			printf("<Saveing flat-field coefficients, please wait...>\n");
			return __SaveDeviceFFC(strFFCPath);
		}
		else
		{
			GX_NODE_ACCESS_MODE emAccessMode;
			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCFlashSave", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			if (!bSupport)
			{
				return false;
			} 

			emStatus = GXSetCommandValue(m_hDevice, "FFCFlashSave");
			GX_VERIFY_BREAK(emStatus);

			return true;
		}
	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		return false;
	}

	return true;
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
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	bool bSupport = false;

	do
	{
		if (!strFFCPath.empty())
		{
			printf("<Loading flat-field coefficients, please wait...>\n");
			return __LoadDeviceFFC(strFFCPath);
		}
		else
		{
			GX_NODE_ACCESS_MODE emAccessMode;
			emStatus = GXGetNodeAccessMode(m_hDevice, "FFCFlashLoad", &emAccessMode);
			GX_VERIFY_BREAK(emStatus);

			bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) 
				|| (emAccessMode == GX_NODE_ACCESS_MODE_WO))
				? true : false;

			if (!bSupport)
			{
				return false;
			} 

			emStatus = GXSetCommandValue(m_hDevice, "FFCFlashLoad");
			GX_VERIFY_BREAK(emStatus);

			return true;
		}
	}while(false);

	if(GX_STATUS_SUCCESS != emStatus)
	{
		__GetErrorString(emStatus);

		return false;
	}

	return true;
}