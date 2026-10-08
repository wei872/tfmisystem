#include "stdafx.h"
#include "GxImageProcess.h"
#include <shlobj.h> 

//---------------------------------------------------------------------------------
/**
\brief   图像拆分重组
\param  [in]vecInput	输入图像列表
\param  [in]nBindSize	Bind个数
\param  [in]nROIHeight	ROI高度
\param  [out]vecOutput	输出图像列表
\return  是否重组成功
*/
//----------------------------------------------------------------------------------
bool CGxImageProcess::DivideImage(std::vector<CImageDataPointer>& vecInput,
	const uint64_t& nROIHeight, std::vector<IMAGE_INFO>& vecOutput)
{
	if (vecInput.empty())
	{
		return false;
	}

	const int64_t PIXEL = 3;
	uint64_t nHeight = vecInput[0]->GetHeight();
	uint64_t nWidth = vecInput[0]->GetWidth();

	std::vector<void*> vecRGB;
	vecRGB.resize(vecInput.size());

	try
	{
		//转换图像
		for (int i = 0; i < vecInput.size(); ++i)
		{
			void* pBuffer = vecInput[i]->ConvertToRGB24(GX_BIT_0_7, GX_RAW2RGB_ADAPTIVE, true);
			vecRGB[i] = pBuffer;
		}
	}
	catch (CGalaxyException &e)
	{
		return false;
	}
	catch (std::exception &e)
	{
		return false;
	}

	//拆分重组
	for (int i = 0; i < vecOutput.size(); ++i)
	{
		for (int j = 0; j < vecRGB.size(); ++j)
		{
			memcpy((char*)vecOutput[i].pImage + (j*nROIHeight*nWidth*PIXEL),
				(char*)vecRGB[j] + (i*nROIHeight*nWidth*PIXEL), nROIHeight* nWidth * PIXEL);
		}
		vecOutput[i].nWidth = nWidth;
		vecOutput[i].nHeight = vecRGB.size()* nROIHeight;
	}

	// 先翻转
	std::reverse(vecOutput.begin(), vecOutput.end());  
	return true;
}

//---------------------------------------------------------------------------------
/**
\brief   图像对齐
\param  [in]vecInput	输入图像列表
\param  [in]vecGapValue	Gap偏移
\param  [out]vecOutput	输出图像列表
\return  是否对齐成功
*/
//----------------------------------------------------------------------------------
bool CGxImageProcess::MatchAndAlign(const std::vector<IMAGE_INFO>& vecInput, 
					const std::vector<uint64_t>& vecGapValue,std::vector<IMAGE_INFO>& vecOutput)
{
	if (vecInput.size() > 4 || vecInput.empty())
	{
		return false;
	}

	if (vecInput.size() == 1)
	{
		vecOutput.emplace_back(vecInput[0]);
		return true;
	}

	if (vecInput.back().nHeight <= vecGapValue.back())
	{
		return false;
	}

	const int64_t PIXEL = 3;
	vecOutput.resize(vecInput.size());
	uint64_t nOutputHeight = vecInput.back().nHeight - vecGapValue.back();
	for (int i = 0; i < vecInput.size(); ++i)
	{
		IMAGE_INFO stImageInfo;
		stImageInfo.pImage = NULL;
		stImageInfo.nWidth = vecInput[i].nWidth;
		stImageInfo.nHeight = nOutputHeight;
		if (i == 0)
		{
			stImageInfo.pImage = vecInput[i].pImage;
		}
		else
		{
			stImageInfo.pImage = (char*)vecInput[i].pImage + vecGapValue[i - 1] * vecInput[i].nWidth*PIXEL;
		}
		vecOutput[i] = stImageInfo;
	}

	return true;
}

//---------------------------------------------------------------------------------
/**
\brief   显示RGB24图像
\param  [in]pDC	        句柄
\param  [in]drawRect	绘制区域
\param  [in]pData	数据
\param  [in]width	宽度
\param  [in]height	高度
\return  是否对齐成功
*/
//----------------------------------------------------------------------------------
void CGxImageProcess::DisplayRGB24Image(CDC * pDC, CRect drawRect, void * pData, const int64_t& width, const int64_t& height)
{
	BITMAPINFO bmi = {};
	bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
	bmi.bmiHeader.biWidth = width;
	bmi.bmiHeader.biHeight = -height; // 负数表示图像自上而下排列
	bmi.bmiHeader.biPlanes = 1;
	bmi.bmiHeader.biBitCount = 24;
	bmi.bmiHeader.biCompression = BI_RGB;

	::StretchDIBits(
		pDC->GetSafeHdc(),
		drawRect.left, drawRect.top,
		drawRect.Width(), drawRect.Height(),
		0, 0, width, height,
		pData,
		&bmi,
		DIB_RGB_COLORS,
		SRCCOPY
	);
}

//---------------------------------------------------------------------------------
/**
\brief   保存图像为BMP
\param  [in]filePath	文件路径
\param  [in]pData		图像数据
\param  [in]width		宽度
\param  [in]height		高度
\return  是否对齐成功
*/
//----------------------------------------------------------------------------------
bool CGxImageProcess::SaveRGB24ToBMP(const CString & filePath, void * pData, const int64_t& width, const int64_t& height)
{
	int bytesPerPixel = 3;
	int lineBytes = ((width * bytesPerPixel + 3) / 4) * 4; // 4 字节对齐
	int imageSize = lineBytes * height;

	BITMAPFILEHEADER bfh = {};
	bfh.bfType = 0x4D42; // 'BM'
	bfh.bfOffBits = sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER);
	bfh.bfSize = bfh.bfOffBits + imageSize;

	BITMAPINFOHEADER bih = {};
	bih.biSize = sizeof(BITMAPINFOHEADER);
	bih.biWidth = width;
	bih.biHeight = -height; // 正数表示从下往上
	bih.biPlanes = 1;
	bih.biBitCount = 24;
	bih.biCompression = BI_RGB;
	bih.biSizeImage = imageSize;

	CString dirPath = filePath.Left(filePath.ReverseFind(_T('\\')));
	SHCreateDirectoryEx(NULL, dirPath, NULL);

	CFile file;
	if (!file.Open(filePath, CFile::modeCreate | CFile::modeWrite | CFile::typeBinary))
		return false;

	file.Write(&bfh, sizeof(bfh));
	file.Write(&bih, sizeof(bih));

	BYTE* pSrc = (BYTE*)pData;
	BYTE* lineBuffer = new BYTE[lineBytes];

	for (int64_t y = height - 1; y >= 0; --y)
	{
		memcpy(lineBuffer, pSrc + y * width * bytesPerPixel, width * bytesPerPixel);
		memset(lineBuffer + width * bytesPerPixel, 0, lineBytes - width * bytesPerPixel);
		file.Write(lineBuffer, lineBytes);
	}

	delete[] lineBuffer;
	file.Close();

	return true;
}


