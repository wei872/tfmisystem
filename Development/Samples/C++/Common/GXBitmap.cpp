//------------------------------------------------------------------------
/**
\file		GXBitmap.cpp
\brief		此类主要用于图像的显示和存储，图像显示和存储可以自适应黑白彩色相机，
图像存储可以存储为Bmp、Raw，对图像显示和存储进行了实现

\Date       2024-03-07
\Version    1.1.2403.9071
*/
//------------------------------------------------------------------------
#include "stdafx.h"
#include "GXBitmap.h"


//---------------------------------------------------------------------------------
//**
//\brief   构造函数
//\param   objCGXDevicePointer 图像设备指针
//\param   pWnd 窗体指针
//\return  无
//*/
//----------------------------------------------------------------------------------
CGXBitmap::CGXBitmap(CGXDevicePointer& objCGXDevicePointer,CWnd* pWnd)
:m_pWnd(pWnd)
,m_hDC(NULL)
,m_bIsColor(false)
,m_i64ImageHeight(0)
,m_i64ImageWidth(0)
,m_pBmpInfo(NULL)
,m_pImageBuffer(NULL)
,m_i64PixelFormat(0)
,m_i64RawSize(0)
,m_i64ConvertSize(0)
,m_objCGXDevicePointer(objCGXDevicePointer)
{

	if ((objCGXDevicePointer.IsNull())||(NULL == pWnd))
	{
		throw std::runtime_error("Argument is error");
	}

	HWND hWnd = pWnd->m_hWnd;
	if (!::IsWindow(hWnd))
	{
		throw std::runtime_error("The HWND must be form");
	}

	m_hDC  = ::GetDC(m_pWnd->m_hWnd);
	memset(m_chBmpBuf,0,sizeof(m_chBmpBuf));

	//初始化图像转换对象
	m_pConvert = IGXFactory::GetInstance().CreateImageFormatConvert();

	//初始化图像质量提升对象
	m_pProcess = IGXFactory::GetInstance().CreateImageProcess();

	// 初始化像素格式映射
	__setupMap();

}


//---------------------------------------------------------------------------------
/**
\brief   析构函数

\return  无
*/
//----------------------------------------------------------------------------------
CGXBitmap::~CGXBitmap(void)
{
	CAutoLock objLock(m_objLock);
	//释放pDC
	::ReleaseDC(m_pWnd->m_hWnd, m_hDC);

	if (m_pImageBuffer != NULL)
	{
		delete[] m_pImageBuffer;
		m_pImageBuffer = NULL;
	}
}

//----------------------------------------------------------------------------------
/**
\brief     通过GX_PIXEL_FORMAT_ENTRY获取最优Bit位
\param     emPixelFormatEntry 图像数据格式
\return    最优Bit位
*/
//----------------------------------------------------------------------------------
GX_VALID_BIT_LIST CGXBitmap::GetBestValudBit(GX_PIXEL_FORMAT_ENTRY emPixelFormatEntry) const
{
	GX_VALID_BIT_LIST emValidBits = GX_BIT_0_7;
	switch (emPixelFormatEntry)
	{
		case GX_PIXEL_FORMAT_R8:
		case GX_PIXEL_FORMAT_G8:
		case GX_PIXEL_FORMAT_B8:
		case GX_PIXEL_FORMAT_MONO8:
		case GX_PIXEL_FORMAT_BAYER_GR8:
		case GX_PIXEL_FORMAT_BAYER_RG8:
		case GX_PIXEL_FORMAT_BAYER_GB8:
		case GX_PIXEL_FORMAT_BAYER_BG8:
			{
				emValidBits = GX_BIT_0_7;
				break;
			}
		case GX_PIXEL_FORMAT_MONO10:
		case GX_PIXEL_FORMAT_MONO10_P:
		case GX_PIXEL_FORMAT_MONO10_PACKED:
		case GX_PIXEL_FORMAT_BAYER_GR10:
		case GX_PIXEL_FORMAT_BAYER_RG10:
		case GX_PIXEL_FORMAT_BAYER_GB10:
		case GX_PIXEL_FORMAT_BAYER_BG10:
        case GX_PIXEL_FORMAT_BAYER_BG10_PACKED:
        case GX_PIXEL_FORMAT_BAYER_GB10_PACKED:
        case GX_PIXEL_FORMAT_BAYER_GR10_PACKED:
        case GX_PIXEL_FORMAT_BAYER_RG10_PACKED:
		case GX_PIXEL_FORMAT_BAYER_BG10_P:
		case GX_PIXEL_FORMAT_BAYER_GB10_P:
		case GX_PIXEL_FORMAT_BAYER_GR10_P:
		case GX_PIXEL_FORMAT_BAYER_RG10_P:
			{
				emValidBits = GX_BIT_2_9;
				break;
			}
		case GX_PIXEL_FORMAT_MONO12:
		case GX_PIXEL_FORMAT_MONO12_P:
		case GX_PIXEL_FORMAT_MONO12_PACKED:
		case GX_PIXEL_FORMAT_BAYER_GR12:
		case GX_PIXEL_FORMAT_BAYER_RG12:
		case GX_PIXEL_FORMAT_BAYER_GB12:
		case GX_PIXEL_FORMAT_BAYER_BG12:
        case GX_PIXEL_FORMAT_BAYER_BG12_PACKED:
        case GX_PIXEL_FORMAT_BAYER_GB12_PACKED:
        case GX_PIXEL_FORMAT_BAYER_GR12_PACKED:
        case GX_PIXEL_FORMAT_BAYER_RG12_PACKED:
		case GX_PIXEL_FORMAT_BAYER_BG12_P:
		case GX_PIXEL_FORMAT_BAYER_GB12_P:
		case GX_PIXEL_FORMAT_BAYER_GR12_P:
		case GX_PIXEL_FORMAT_BAYER_RG12_P:
			{
				emValidBits = GX_BIT_4_11;
				break;
			}
		case GX_PIXEL_FORMAT_MONO14:
		case GX_PIXEL_FORMAT_MONO14_P:
		case GX_PIXEL_FORMAT_BAYER_GR14:
		case GX_PIXEL_FORMAT_BAYER_RG14:
		case GX_PIXEL_FORMAT_BAYER_GB14:
		case GX_PIXEL_FORMAT_BAYER_BG14:
		case GX_PIXEL_FORMAT_BAYER_GR14_P:
		case GX_PIXEL_FORMAT_BAYER_RG14_P:
		case GX_PIXEL_FORMAT_BAYER_GB14_P:
		case GX_PIXEL_FORMAT_BAYER_BG14_P:
			{
				emValidBits = GX_BIT_6_13;
				break;
			}
		case GX_PIXEL_FORMAT_MONO16:
		case GX_PIXEL_FORMAT_BAYER_GR16:
		case GX_PIXEL_FORMAT_BAYER_RG16:
		case GX_PIXEL_FORMAT_BAYER_GB16:
		case GX_PIXEL_FORMAT_BAYER_BG16:
			{
				emValidBits = GX_BIT_8_15;
				break;
			}
		default:
			//返回默认值GX_BIT_0_7
			break;
	}
	return emValidBits;
}

//---------------------------------------------------------------------------------
/**
\brief   为彩色相机图像显示准备资源

\return  无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__ColorPrepareForShowImg()
{
	const int32_t BIT_COUNT = 24;
	//---------------------------初始化bitmap头---------------------------
	m_pBmpInfo								= (BITMAPINFO *)m_chBmpBuf;
	m_pBmpInfo->bmiHeader.biSize			= sizeof(BITMAPINFOHEADER);
	m_pBmpInfo->bmiHeader.biWidth			= (LONG)m_i64ImageWidth;
	m_pBmpInfo->bmiHeader.biHeight			= (LONG)m_i64ImageHeight;

	m_pBmpInfo->bmiHeader.biPlanes			= 1;
	m_pBmpInfo->bmiHeader.biBitCount        = BIT_COUNT;
	m_pBmpInfo->bmiHeader.biCompression		= BI_RGB;
	m_pBmpInfo->bmiHeader.biSizeImage		= 0;
	m_pBmpInfo->bmiHeader.biXPelsPerMeter	= 0;
	m_pBmpInfo->bmiHeader.biYPelsPerMeter	= 0;
	m_pBmpInfo->bmiHeader.biClrUsed			= 0;
	m_pBmpInfo->bmiHeader.biClrImportant	= 0;

	//为经过翻转后的图像数据分配空间
	m_pImageBuffer = new(std::nothrow) BYTE[(size_t)(m_i64ImageWidth * m_i64ImageHeight * PIXEL)];
	if (NULL == m_pImageBuffer)
	{
		throw std::runtime_error("Fail to allocate memory");
	}
}

//---------------------------------------------------------------------------------
/**
\brief   为黑白相机图像显示准备资源

\return  无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__MonoPrepareForShowImg()
{
	const int32_t BIT_COUNT = 8;
	//----------------------初始化bitmap头---------------------------------
	m_pBmpInfo								= (BITMAPINFO *)m_chBmpBuf;
	m_pBmpInfo->bmiHeader.biSize			= sizeof(BITMAPINFOHEADER);
	m_pBmpInfo->bmiHeader.biWidth			= (LONG)m_i64ImageWidth;
	m_pBmpInfo->bmiHeader.biHeight			= (LONG)m_i64ImageHeight;	

	m_pBmpInfo->bmiHeader.biPlanes			= 1;
	m_pBmpInfo->bmiHeader.biBitCount		= BIT_COUNT; // 黑白图像为8
	m_pBmpInfo->bmiHeader.biCompression		= BI_RGB;
	m_pBmpInfo->bmiHeader.biSizeImage		= 0;
	m_pBmpInfo->bmiHeader.biXPelsPerMeter	= 0;
	m_pBmpInfo->bmiHeader.biYPelsPerMeter	= 0;
	m_pBmpInfo->bmiHeader.biClrUsed			= 0;
	m_pBmpInfo->bmiHeader.biClrImportant	= 0;

	// 黑白图像需要初始化调色板
	const int32_t PALETTE = 256;
	for(int32_t i32Index=0; i32Index<PALETTE; ++i32Index)
	{
		m_pBmpInfo->bmiColors[i32Index].rgbBlue	     = i32Index;
		m_pBmpInfo->bmiColors[i32Index].rgbGreen	 = i32Index;
		m_pBmpInfo->bmiColors[i32Index].rgbRed		 = i32Index;
		m_pBmpInfo->bmiColors[i32Index].rgbReserved =0;
	}

	//为经过翻转后的图像数据分配空间
	m_pImageBuffer = new(std::nothrow) BYTE[(size_t)(m_i64ImageWidth * m_i64ImageHeight)];
	if (NULL == m_pImageBuffer)
	{
		throw std::runtime_error("Fail to allocate memory");
	}
}

//---------------------------------------------------------------------------------
/**
\brief   将m_pBufferRGB中图像显示到界面
\param   strDeviceSNFPS  设备帧率序列号

\return  无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__DrawImg(char* strDeviceSNFPS)
{
	int32_t i32WndWidth  = 0;
	int32_t i32WndHeight = 0;

	CAutoLock objLock(m_objLock);
	if (NULL == m_pImageBuffer)
	{
		return;
	}

	// 为画图做准备
	RECT objRect;
	m_pWnd->GetClientRect(&objRect);
	i32WndWidth  = objRect.right - objRect.left;
	i32WndHeight = objRect.bottom - objRect.top;

	HDC      objMemDC = ::CreateCompatibleDC(m_hDC);
	HBITMAP  objMemBmp= CreateCompatibleBitmap(m_hDC, i32WndWidth, i32WndHeight);
	::SelectObject(objMemDC,objMemBmp);

	// 必须调用该语句，否则图像出现水纹
	::SetStretchBltMode(objMemDC, COLORONCOLOR);
	::StretchDIBits(objMemDC,
		0,
		0,
		i32WndWidth,
		i32WndHeight,
		0,
		0,
		(int32_t)m_i64ImageWidth,
		(int32_t)m_i64ImageHeight,
		m_pImageBuffer,
		m_pBmpInfo,
		DIB_RGB_COLORS,
		SRCCOPY
		);
	if (NULL != strDeviceSNFPS)
	{
		TextOut(objMemDC, 0, 0, strDeviceSNFPS, (int32_t)strlen(strDeviceSNFPS));
	}
	StretchBlt(m_hDC,
		0,
		0,
		i32WndWidth,
		i32WndHeight,
		objMemDC,
		0,
		0,
		i32WndWidth,
		i32WndHeight,
		SRCCOPY);

	::DeleteDC(objMemDC);
	DeleteObject(objMemBmp);
}

//----------------------------------------------------------------------------------
/**
\brief     计算宽度所占的字节数
\param     nWidth  图像宽度
\param     bIsColor  是否是彩色相机
\return    图像一行所占的字节数
*/
//----------------------------------------------------------------------------------
int64_t CGXBitmap::__GetStride(int64_t i64Width, bool bIsColor) const
{
	return bIsColor ? (i64Width * PIXEL) : i64Width;
}

//----------------------------------------------------------------------------------
/**
\brief     用于显示图像
\param     objCImageDataPointer  图像数据对象
\param     strDeviceSNFPS        图像帧率序列号
\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::Show(CImageDataPointer& objCImageDataPointer,char* pDeviceSNFPS)
{
	if (objCImageDataPointer.IsNull())
	{
		throw std::runtime_error("NULL pointer dereferenced");
	}

	GX_STATUS     emStatus = GX_STATUS_ERROR;
	if (__NeedFilp(static_cast<GX_PIXEL_FORMAT_ENTRY>(m_i64PixelFormat)))
	{
		__FilpImage(objCImageDataPointer->GetBuffer());
	}
	else
	{
		CAutoLock objLock(m_objLock);
		if (NULL == m_pImageBuffer)
		{
			return;
		}
        
		m_pConvert->Convert(objCImageDataPointer, m_pImageBuffer, static_cast<size_t>(m_i64ConvertSize), true);
	}

	// 在屏幕上绘图
	__DrawImg(pDeviceSNFPS);
}

//----------------------------------------------------------------------------------
/**
\brief     用于图像处理后并显示图像
\param     objCfg  图像处理调节参数对象
\param     objCImageDataPointer  图像数据对象
\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::ShowImageProcess(CImageProcessConfigPointer& objCfg, CImageDataPointer& objCImageDataPointer)
{
	if ((objCfg.IsNull())||(objCImageDataPointer.IsNull()))
	{
		throw std::runtime_error("NULL pointer dereferenced");
	}

	CAutoLock objLock(m_objLock);
	if (NULL == m_pImageBuffer)
	{
		return;
	}
	objCfg->EnableConvertFlip(true);
	m_pProcess->ImageImprovment(objCImageDataPointer, m_pImageBuffer, objCfg);

	__DrawImg();

}

//----------------------------------------------------------------------------------
/**
\brief     存储Bmp图像
\param     objCImageDataPointer  图像数据对象
\param     strFilePath  显示图像文件名
\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::SaveBmp(const std::string& strFilePath) const
{
	const uint32_t FILE_TYPE = 8;

	DWORD		         dwImageSize = static_cast<DWORD>(__GetStride(m_i64ImageWidth, m_bIsColor) * m_i64ImageHeight);
	BITMAPFILEHEADER     stBfh	     = {0};
	DWORD		         dwBytesRead = 0;

	stBfh.bfType	= (WORD)'M' << FILE_TYPE | 'B';			 //定义文件类型
	stBfh.bfOffBits = m_bIsColor ?sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER)
		:sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER) + (256 * 4);	//定义文件头大小true为彩色,false为黑白
	stBfh.bfSize	= stBfh.bfOffBits + dwImageSize; //文件大小

	DWORD dwBitmapInfoHeader = m_bIsColor ?sizeof(BITMAPINFOHEADER)
		:sizeof(BITMAPINFOHEADER) + (256 * 4);	//定义BitmapInfoHeader大小true为彩色,false为黑白

	//创建文件
	HANDLE hFile = ::CreateFile(strFilePath.c_str(),
		GENERIC_WRITE,
		0,
		NULL,
		CREATE_ALWAYS,														
		FILE_ATTRIBUTE_NORMAL,
		NULL);

	if (hFile == INVALID_HANDLE_VALUE) 
	{
		throw std::runtime_error("Handle is invalid");
	}

	::WriteFile(hFile, &stBfh, sizeof(BITMAPFILEHEADER), &dwBytesRead, NULL);
	::WriteFile(hFile, m_pBmpInfo, dwBitmapInfoHeader, &dwBytesRead, NULL); //黑白和彩色自适应
	::WriteFile(hFile, m_pImageBuffer, dwImageSize, &dwBytesRead, NULL);

	CloseHandle(hFile);
}

//----------------------------------------------------------------------------------
/**
\brief     存储Raw图像
\param     objCImageDataPointer  图像数据对象
\param     strFilePath  显示图像文件名
\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::SaveRaw(CImageDataPointer& objCImageDataPointer,const std::string& strFilePath) const
{
	if ((objCImageDataPointer.IsNull())||(strFilePath == ""))
	{
		throw std::runtime_error("Argument is error");
	}

	DWORD   dwImageSize = (DWORD)objCImageDataPointer->GetPayloadSize();  // 写入文件的长度
	DWORD   dwBytesRead = 0;                // 文件读取的长度

	BYTE* pbuffer = (BYTE*)objCImageDataPointer->GetBuffer();
	// 创建文件
	HANDLE hFile = ::CreateFile(strFilePath.c_str(),
		GENERIC_WRITE,
		FILE_SHARE_READ,
		NULL,
		CREATE_ALWAYS,														
		FILE_ATTRIBUTE_NORMAL,
		NULL);

	if (hFile == INVALID_HANDLE_VALUE)   // 创建失败则返回
	{
		throw std::runtime_error("Handle is invalid");
	}
	else                                 // 保存Raw图像          
	{ 
		::WriteFile(hFile, pbuffer, dwImageSize, &dwBytesRead, NULL);
		CloseHandle(hFile);
	}
}

//----------------------------------------------------------------------------------
/**
\brief     当前像素是否为彩色
\param     objCGXDevicePointer  [in]    设备句柄
\param     bIsColorFilter       [out]   是否支持彩色

\return
*/
//----------------------------------------------------------------------------------
void CGXBitmap::IsColor(CGXDevicePointer& objCGXDevicePointer, bool &bIsColorFilter) const
{
	const std::string  strPixelFormat = objCGXDevicePointer->GetRemoteFeatureControl()
		->GetEnumFeature("PixelFormat")->GetValue().c_str();

	GX_PIXEL_FORMAT_ENTRY i32Pixel = static_cast<GX_PIXEL_FORMAT_ENTRY>(__ConvertPixelFormatToInt(strPixelFormat));

	//将图像格式和下述宏定义做按位与（&）运算，可判断像素格式是mono还是RGB
	const int32_t i32PixelMono = 0x01000000;               //判断是否为MONO格式的掩码
	const int32_t i32PixelRgb = 0x20000000;                //判断是否为RGB格式的掩码 
	const int32_t i32PixelMonoRgbCustom = 0x80000000;      //判断是否为MONO格式的掩码 
	const int32_t i32PixelColorMask = 0xFF000000;          //判断是否为彩色格式的掩码

	//将图像格式与下述宏定义做按位与（&）运算，可得到像素格式的ID
	int32_t i32PixelIdMask = 0x0000FFFF;

	bool bIsMono = ((i32PixelColorMask & i32Pixel) == i32PixelMono); // 是否为mono格式

	bool bIsRgb = ((i32PixelColorMask & i32Pixel) == i32PixelRgb);  // 是否为RGB格式           
    bool bIsBayer = __IsBayer(i32Pixel);   // 是否为Bayer格式

	bIsColorFilter = !(bIsMono && (!bIsBayer) && (!bIsRgb));  // 用于判断是否为黑白相机
}

//----------------------------------------------------------------------------------
/**
\brief     字符型像素格式转换成整型

\return    整型像素格式
*/
//----------------------------------------------------------------------------------
int64_t CGXBitmap::__ConvertPixelFormatToInt(std::string PixelFormat) const
{
	std::map<std::string, int64_t>::const_iterator iter = m_mapPixelFormat.find(PixelFormat);
	if (iter != m_mapPixelFormat.end())
	{
		return iter->second;
	}
	else
	{
		throw std::runtime_error("Format Undefined");
	}
}

//---------------------------------------------------------------------------------
/**
\brief   为相机图像显示准备资源

\return  无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::PrepareForShowImg()
{
	// 释放旧内存, 原因是停止采集操作和回调函数是异步的
	// 若在停止采集后直接释放内存则可能因为回调函数还在运行导致程序崩溃
	// 所以改为在开始采集时释放旧内存
	UnPrepareForShowImg();

	// 更新图像的宽、高、像素格式、转换后的像素格式等
	__GetBasicAttribute();

	// 设置图像转换句柄
	__SetConvertHandle();

	if (m_bIsColor)
	{
		__ColorPrepareForShowImg();
	} 
	else
	{
		__MonoPrepareForShowImg();
	}

}

//----------------------------------------------------------------------------------
/**
\brief     释放为图像显示准备的资源

\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::UnPrepareForShowImg()
{
	CAutoLock objLock(m_objLock);
	if(NULL != m_pImageBuffer)
	{
		delete[] m_pImageBuffer;
		m_pImageBuffer = NULL;
	}
}

//----------------------------------------------------------------------------------
/**
\brief     得到整型的当前像素格式

\return    整型的当前像素格式
*/
//----------------------------------------------------------------------------------
int64_t CGXBitmap::GetCurrentPixelFormat()
{
	//获取当前像素格式
	if (0 == m_i64PixelFormat)
	{
		bool bIsLocal = m_objCGXDevicePointer->GetFeatureControl()->IsImplemented("OutPixelFormat");
		if (bIsLocal)
		{
			std::string  strPixelFormat
				= m_objCGXDevicePointer->GetFeatureControl()->GetEnumFeature("OutPixelFormat")->GetValue().c_str();
			m_i64PixelFormat = __ConvertPixelFormatToInt(strPixelFormat);
		}
		else
		{
			std::string  strPixelFormat 
				= m_objCGXDevicePointer->GetRemoteFeatureControl()->GetEnumFeature("PixelFormat")->GetValue().c_str();
			m_i64PixelFormat = __ConvertPixelFormatToInt(strPixelFormat);
		}
	}
	return m_i64PixelFormat;
}

//----------------------------------------------------------------------------------
/**
\brief     得到相机采图的宽高像素格式等属性

\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__GetBasicAttribute()
{
	//获得图像宽度、高度等
	m_i64ImageWidth = (int64_t)m_objCGXDevicePointer->GetRemoteFeatureControl()->GetIntFeature("Width")->GetValue();
	m_i64ImageHeight = (int64_t)m_objCGXDevicePointer->GetRemoteFeatureControl()->GetIntFeature("Height")->GetValue();

	//获取当前像素格式
	bool bIsLocal = m_objCGXDevicePointer->GetFeatureControl()->IsImplemented("OutPixelFormat");
	if(bIsLocal)
	{
		std::string  strPixelFormat = 
			m_objCGXDevicePointer->GetFeatureControl()->GetEnumFeature("OutPixelFormat")->GetValue().c_str();
		m_i64PixelFormat = __ConvertPixelFormatToInt(strPixelFormat);
	}else
	{
		std::string  strPixelFormat = 
			m_objCGXDevicePointer->GetRemoteFeatureControl()->GetEnumFeature("PixelFormat")->GetValue().c_str();
		m_i64PixelFormat = __ConvertPixelFormatToInt(strPixelFormat);
	}
	
	//获取当前像素格式是否为彩色
	IsColor(m_objCGXDevicePointer,m_bIsColor);

}

//----------------------------------------------------------------------------------
/**
\brief     设置图像转换句柄
\param     hConvertHandle 图像转换句柄
\param     emInputPixelForma 输入图像的像素格式

\return    true为8为数据，false为非8位数据
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__SetConvertHandle()
{
	// 设置插值方式
	m_pConvert->SetInterpolationType(GX_RAW2RGB_NEIGHBOUR);

	// 获取有效位数
	GX_VALID_BIT_LIST emValidBits = GetBestValudBit(static_cast<GX_PIXEL_FORMAT_ENTRY>(m_i64PixelFormat));

	// 设置有效位数
	m_pConvert->SetValidBits(emValidBits);

	if (m_bIsColor)
	{
		// 设置图像格式转换句柄，转换为BGR8格式
		m_pConvert->SetDstFormat(GX_PIXEL_FORMAT_BGR8);
		m_i64ConvertSize = m_pConvert->GetBufferSizeForConversion(m_i64ImageWidth, m_i64ImageHeight, GX_PIXEL_FORMAT_BGR8);
	}
	else
	{
		// 设置图像格式转换句柄，转换为RGB8格式
		m_pConvert->SetDstFormat(GX_PIXEL_FORMAT_MONO8);
		m_i64ConvertSize = m_pConvert->GetBufferSizeForConversion(m_i64ImageWidth, m_i64ImageHeight, GX_PIXEL_FORMAT_MONO8);
	}

	//计算转换前后图像的大小
	m_i64RawSize = m_pConvert->GetBufferSizeForConversion(m_i64ImageWidth, m_i64ImageHeight
		, static_cast<GX_PIXEL_FORMAT_ENTRY>(m_i64PixelFormat));
}

//----------------------------------------------------------------------------------
/**
\brief     翻转图像buffer

\return    无
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__FilpImage(void* pRawImageBuffer)
{
	CAutoLock objLock(m_objLock);
	if ((NULL == m_pImageBuffer)
		|| (NULL == pRawImageBuffer))
	{
		return;
	}
	BYTE* pRawBuffer = reinterpret_cast<BYTE*>(pRawImageBuffer);
	if (GX_PIXEL_FORMAT_BGR8 == m_i64PixelFormat)
	{
		// RGB格式需要翻转数据后显示
		for (int32_t i32Index = 0; i32Index < m_i64ImageHeight; ++i32Index)
		{
			memcpy(m_pImageBuffer + i32Index * m_i64ImageWidth * PIXEL
				, pRawBuffer + (m_i64ImageHeight - i32Index - 1) * m_i64ImageWidth * PIXEL, (size_t)m_i64ImageWidth * PIXEL);
		}
	}
	else if ((GX_PIXEL_FORMAT_R8 == m_i64PixelFormat)
		|| (GX_PIXEL_FORMAT_G8 == m_i64PixelFormat)
		|| (GX_PIXEL_FORMAT_B8 == m_i64PixelFormat)
		|| (GX_PIXEL_FORMAT_MONO8 == m_i64PixelFormat))
	{
		// 黑白相机需要翻转数据后显示
		for (int32_t i32Index = 0; i32Index < m_i64ImageHeight; ++i32Index)
		{
			memcpy(m_pImageBuffer + i32Index * m_i64ImageWidth
				, pRawBuffer + (m_i64ImageHeight - i32Index - 1) * m_i64ImageWidth, (size_t)m_i64ImageWidth);
		}
	}
	else 
	{
	
	}
}

//----------------------------------------------------------------------------------
/**
\brief     判断图像是否需要反转

\return    true需要反转，false不需要反转
*/
//----------------------------------------------------------------------------------
bool CGXBitmap::__NeedFilp(GX_PIXEL_FORMAT_ENTRY emPixelFormat)const
{
	if ((GX_PIXEL_FORMAT_BGR8 == emPixelFormat)
		||(GX_PIXEL_FORMAT_R8 == emPixelFormat)
		|| (GX_PIXEL_FORMAT_G8 == emPixelFormat)
		|| (GX_PIXEL_FORMAT_B8 == emPixelFormat)
		|| (GX_PIXEL_FORMAT_MONO8 == emPixelFormat))
	{
		return true;
	}
	return false;
}

//----------------------------------------------------------------------------------
/**
\brief  初始化关联map
\param  
*/
//----------------------------------------------------------------------------------
void CGXBitmap::__setupMap()
{
	m_mapPixelFormat["R8"] = GX_PIXEL_FORMAT_R8;
	m_mapPixelFormat["G8"] = GX_PIXEL_FORMAT_G8;
	m_mapPixelFormat["B8"] = GX_PIXEL_FORMAT_B8;
	m_mapPixelFormat["Mono8"] = GX_PIXEL_FORMAT_MONO8;
	m_mapPixelFormat["Mono10"] = GX_PIXEL_FORMAT_MONO10;
	m_mapPixelFormat["Mono10Packed"] = GX_PIXEL_FORMAT_MONO10_PACKED;
	m_mapPixelFormat["Mono12"] = GX_PIXEL_FORMAT_MONO12;
	m_mapPixelFormat["Mono12Packed"] = GX_PIXEL_FORMAT_MONO12_PACKED;
	m_mapPixelFormat["Mono14"] = GX_PIXEL_FORMAT_MONO14;
	m_mapPixelFormat["Mono16"] = GX_PIXEL_FORMAT_MONO16;
	m_mapPixelFormat["Mono10p"] = GX_PIXEL_FORMAT_MONO10_P;
	m_mapPixelFormat["Mono12p"] = GX_PIXEL_FORMAT_MONO12_P;
	m_mapPixelFormat["Mono14p"] = GX_PIXEL_FORMAT_MONO14_P;
	m_mapPixelFormat["BayerGR8"] = GX_PIXEL_FORMAT_BAYER_GR8;
	m_mapPixelFormat["BayerRG8"] = GX_PIXEL_FORMAT_BAYER_RG8;
	m_mapPixelFormat["BayerGB8"] = GX_PIXEL_FORMAT_BAYER_GB8;
	m_mapPixelFormat["BayerBG8"] = GX_PIXEL_FORMAT_BAYER_BG8;
	m_mapPixelFormat["BayerGR10"] = GX_PIXEL_FORMAT_BAYER_GR10;
	m_mapPixelFormat["BayerRG10"] = GX_PIXEL_FORMAT_BAYER_RG10;
	m_mapPixelFormat["BayerGB10"] = GX_PIXEL_FORMAT_BAYER_GB10;
	m_mapPixelFormat["BayerBG10"] = GX_PIXEL_FORMAT_BAYER_BG10;
	m_mapPixelFormat["BayerGR10p"] = GX_PIXEL_FORMAT_BAYER_GR10_P;
	m_mapPixelFormat["BayerRG10p"] = GX_PIXEL_FORMAT_BAYER_RG10_P;
	m_mapPixelFormat["BayerGB10p"] = GX_PIXEL_FORMAT_BAYER_GB10_P;
	m_mapPixelFormat["BayerBG10p"] = GX_PIXEL_FORMAT_BAYER_BG10_P;
	m_mapPixelFormat["BayerGR12"] = GX_PIXEL_FORMAT_BAYER_GR12;
	m_mapPixelFormat["BayerRG12"] = GX_PIXEL_FORMAT_BAYER_RG12;
	m_mapPixelFormat["BayerGB12"] = GX_PIXEL_FORMAT_BAYER_GB12;
	m_mapPixelFormat["BayerBG12"] = GX_PIXEL_FORMAT_BAYER_BG12;
	m_mapPixelFormat["BayerGR12p"] = GX_PIXEL_FORMAT_BAYER_GR12_P;
	m_mapPixelFormat["BayerRG12p"] = GX_PIXEL_FORMAT_BAYER_RG12_P;
	m_mapPixelFormat["BayerGB12p"] = GX_PIXEL_FORMAT_BAYER_GB12_P;
	m_mapPixelFormat["BayerBG12p"] = GX_PIXEL_FORMAT_BAYER_BG12_P;
	m_mapPixelFormat["BayerGR14"] = GX_PIXEL_FORMAT_BAYER_GR14;
	m_mapPixelFormat["BayerRG14"] = GX_PIXEL_FORMAT_BAYER_RG14;
	m_mapPixelFormat["BayerGB14"] = GX_PIXEL_FORMAT_BAYER_GB14;
	m_mapPixelFormat["BayerBG14"] = GX_PIXEL_FORMAT_BAYER_BG14;
	m_mapPixelFormat["BayerGR14p"] = GX_PIXEL_FORMAT_BAYER_GR14_P;
	m_mapPixelFormat["BayerRG14p"] = GX_PIXEL_FORMAT_BAYER_RG14_P;
	m_mapPixelFormat["BayerGB14p"] = GX_PIXEL_FORMAT_BAYER_GB14_P;
	m_mapPixelFormat["BayerBG14p"] = GX_PIXEL_FORMAT_BAYER_BG14_P;
	m_mapPixelFormat["BayerGR16"] = GX_PIXEL_FORMAT_BAYER_GR16;
	m_mapPixelFormat["BayerRG16"] = GX_PIXEL_FORMAT_BAYER_RG16;
	m_mapPixelFormat["BayerGB16"] = GX_PIXEL_FORMAT_BAYER_GB16;
	m_mapPixelFormat["BayerBG16"] = GX_PIXEL_FORMAT_BAYER_BG16;
	m_mapPixelFormat["RGB8"] = GX_PIXEL_FORMAT_RGB8;
	m_mapPixelFormat["BGR8"] = GX_PIXEL_FORMAT_BGR8;
	m_mapPixelFormat["BayerBG10Packed"] = GX_PIXEL_FORMAT_BAYER_BG10_PACKED;
	m_mapPixelFormat["BayerBG12Packed"] = GX_PIXEL_FORMAT_BAYER_BG12_PACKED;
	m_mapPixelFormat["BayerGB10Packed"] = GX_PIXEL_FORMAT_BAYER_GB10_PACKED;
	m_mapPixelFormat["BayerGB12Packed"] = GX_PIXEL_FORMAT_BAYER_GB12_PACKED;
	m_mapPixelFormat["BayerGR10Packed"] = GX_PIXEL_FORMAT_BAYER_GR10_PACKED;
	m_mapPixelFormat["BayerGR12Packed"] = GX_PIXEL_FORMAT_BAYER_GR12_PACKED;
	m_mapPixelFormat["BayerRG10Packed"] = GX_PIXEL_FORMAT_BAYER_RG10_PACKED;
	m_mapPixelFormat["BayerRG12Packed"] = GX_PIXEL_FORMAT_BAYER_RG12_PACKED;
    m_mapPixelFormat["YUV422_8"] = GX_PIXEL_FORMAT_YUV422_8;
    m_mapPixelFormat["YUV422_8_UYVY"] = GX_PIXEL_FORMAT_YUV422_8_UYVY;
}

//----------------------------------------------------------------------------------
/**
\brief     判断图像是否为Bayer格式

\return    true是Bayer格式，false不是Bayer格式
*/
//----------------------------------------------------------------------------------
bool CGXBitmap::__IsBayer(GX_PIXEL_FORMAT_ENTRY nPixelFormat) const
{
	bool bIsBayer = false;
	switch (nPixelFormat)
	{
	case GX_PIXEL_FORMAT_BAYER_GR8:
	case GX_PIXEL_FORMAT_BAYER_RG8:
	case GX_PIXEL_FORMAT_BAYER_GB8:
	case GX_PIXEL_FORMAT_BAYER_BG8:
	case GX_PIXEL_FORMAT_BAYER_GR10:
	case GX_PIXEL_FORMAT_BAYER_RG10:
	case GX_PIXEL_FORMAT_BAYER_GB10:
	case GX_PIXEL_FORMAT_BAYER_BG10:
	case GX_PIXEL_FORMAT_BAYER_GR12:
	case GX_PIXEL_FORMAT_BAYER_RG12:
	case GX_PIXEL_FORMAT_BAYER_GB12:
	case GX_PIXEL_FORMAT_BAYER_BG12:
	case GX_PIXEL_FORMAT_BAYER_GR14:
	case GX_PIXEL_FORMAT_BAYER_RG14:
	case GX_PIXEL_FORMAT_BAYER_GB14:
	case GX_PIXEL_FORMAT_BAYER_BG14:
	case GX_PIXEL_FORMAT_BAYER_GR16:
	case GX_PIXEL_FORMAT_BAYER_RG16:
	case GX_PIXEL_FORMAT_BAYER_GB16:
	case GX_PIXEL_FORMAT_BAYER_BG16:
	case GX_PIXEL_FORMAT_BAYER_GR10_P:
	case GX_PIXEL_FORMAT_BAYER_RG10_P:
	case GX_PIXEL_FORMAT_BAYER_GB10_P:
	case GX_PIXEL_FORMAT_BAYER_BG10_P:
	case GX_PIXEL_FORMAT_BAYER_GR12_P:
	case GX_PIXEL_FORMAT_BAYER_RG12_P:
	case GX_PIXEL_FORMAT_BAYER_GB12_P:
	case GX_PIXEL_FORMAT_BAYER_BG12_P:
	case GX_PIXEL_FORMAT_BAYER_GR14_P:
	case GX_PIXEL_FORMAT_BAYER_RG14_P:
	case GX_PIXEL_FORMAT_BAYER_GB14_P:
	case GX_PIXEL_FORMAT_BAYER_BG14_P:
	case GX_PIXEL_FORMAT_BAYER_BG10_PACKED:
	case GX_PIXEL_FORMAT_BAYER_GB10_PACKED:
	case GX_PIXEL_FORMAT_BAYER_GR10_PACKED:
	case GX_PIXEL_FORMAT_BAYER_RG10_PACKED:
	case GX_PIXEL_FORMAT_BAYER_BG12_PACKED:
	case GX_PIXEL_FORMAT_BAYER_GB12_PACKED:
	case GX_PIXEL_FORMAT_BAYER_GR12_PACKED:
	case GX_PIXEL_FORMAT_BAYER_RG12_PACKED:
		bIsBayer = true;
		break;
	}

	return bIsBayer;
}
