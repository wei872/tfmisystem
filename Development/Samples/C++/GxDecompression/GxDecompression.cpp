#include <iostream>
#include "stdafx.h"
#include <iomanip>
#include "GalaxyIncludes.h"

using namespace std;

// 打印设备信息
void PrintDeviceInfo(CGXDevicePointer& pDevice)
{
	cout << "***********************************************" << endl;
	cout << "<Vendor Name:   " << pDevice->GetDeviceInfo().GetVendorName() << ">" << endl;
	cout << "<Model Name:    " << pDevice->GetDeviceInfo().GetModelName() << ">" << endl;
	cout << "<Serial Number: " << pDevice->GetDeviceInfo().GetSN() << ">" << endl;
	cout << "***********************************************" << endl << endl;
}


int main(int argc, char* argv[])
{
    char* pDecompressionBuf = NULL;

	try
	{
		//初始化设备库
		IGXFactory::GetInstance().Init();

		//枚举相机设备
		GxIAPICPP::gxdeviceinfo_vector vectorDeviceInfo;
		IGXFactory::GetInstance().UpdateDeviceList(1000, vectorDeviceInfo);

		//判断当前设备连接个数
		if (vectorDeviceInfo.size() <= 0)
		{
			cout << "No device!" << endl;
			cout << "<App exit!>"<< endl;
			system("pause");
			return 0;
		}

		//通过SN打开相机设备
		CGXDevicePointer pDevice = IGXFactory::GetInstance().OpenDeviceBySN(vectorDeviceInfo[0].GetSN(), GX_ACCESS_EXCLUSIVE);
		//获取相机属性控制对象
		CGXFeatureControlPointer pRemoteFeatureControl = pDevice->GetRemoteFeatureControl();
		//流层对象
		CGXStreamPointer pStream;
		if (pDevice->GetStreamCount() > 0)
		{
			pStream = pDevice->OpenStream(0);
		}
		else
		{
			cout << "Not find stream!"<< endl;
			cout << "<App exit!>"<< endl;
			system("pause");
			return 0;
		}
		
		//选择默认参数组
        pRemoteFeatureControl->GetEnumFeature("UserSetSelector")->SetValue("Default");
		//加载参数组
        pRemoteFeatureControl->GetCommandFeature("UserSetLoad")->Execute();

		PrintDeviceInfo(pDevice);

		if( !pRemoteFeatureControl->IsImplemented( "ImageCompressionMode")
			|| !pRemoteFeatureControl->IsReadable("ImageCompressionMode")
			|| !pRemoteFeatureControl->IsWritable("ImageCompressionMode"))
		{
			cout << "This device  does not support compression function!" << endl;
			cout << "<App exit!>"<< endl;
			system("pause");
			return 0;
		}
		
		bool bIsLossless = false;
		gxstring_vector vecEntry = pRemoteFeatureControl->GetEnumFeature("ImageCompressionMode")->GetEnumEntryList();
		for ( int nIndex = 0; nIndex < vecEntry.size(); ++nIndex)
		{
			if ( vecEntry[nIndex].compare( "Lossless") == 0)
			{
				bIsLossless = true;
			}
		}

		if ( !bIsLossless)
		{
			cout << "This device does not support lossless compression function!" << endl;
			cout << "<App exit!>"<< endl;
			system("pause");
			return 0;
		}

        pRemoteFeatureControl->GetEnumFeature("ImageCompressionMode")->SetValue("Lossless");
		int nImgMethod = pRemoteFeatureControl->GetEnumFeature("ImageCompressionMethod")->GetEnumValue().nCurValue;
		int nImgWidth = pRemoteFeatureControl->GetIntFeature("Width")->GetValue();
		int nImgHeight = pRemoteFeatureControl->GetIntFeature("Height")->GetValue();
        GX_PIXEL_FORMAT_ENTRY nImgPixelFormat = static_cast<GX_PIXEL_FORMAT_ENTRY>(pRemoteFeatureControl->GetEnumFeature("PixelFormat")->GetEnumValue().nCurValue);
		uint64_t nPayloadSize = pRemoteFeatureControl->GetIntFeature("PayloadSize")->GetValue();

		pDecompressionBuf = new char[nPayloadSize];
		if (NULL == pDecompressionBuf)
		{
			cout << "Memory allocation failed!" << endl;
			cout << "<App exit!>"<< endl;
			system("pause");
            return 0;
		}
		memset(pDecompressionBuf, 0, nPayloadSize);

        CGXDecompressorPointer objDecompressor = IGXFactory::GetInstance().CreateDecompressor();

		//开启流层采集
		pStream->StartGrab();
		//开启相机采集
        pRemoteFeatureControl->GetCommandFeature("AcquisitionStart")->Execute();

		unsigned int nImageNum = 10;
		while( nImageNum-- > 0)
		{
			try
			{
                uint64_t ui64DecompressionBufSize = nPayloadSize;
				//零拷贝采集一帧图像
				CImageDataPointer pImgData = pStream->DQBuf(1000);
				if (GX_FRAME_STATUS_SUCCESS == pImgData->GetStatus())
				{
                    objDecompressor->Decompression(pImgData->GetBuffer(), pImgData->GetPayloadSize(), pDecompressionBuf, 
                        &ui64DecompressionBufSize, nImgPixelFormat, nImgWidth, nImgHeight, nImgMethod);

                    cout << "FrameID: " << pImgData->GetFrameID() << std::fixed << std::setprecision(2) <<
                        "   Compression ratio: " << (float)pImgData->GetPayloadSize() / (float)ui64DecompressionBufSize << endl;
				}
				else
				{
					cout << "Abnormal Acquisition: Exception code: " << pImgData->GetStatus() << endl;
				}

				//将采集图像buffer还回到采集系统
				pStream->QBuf(pImgData);
			}
			catch (CGalaxyException &e)
			{
				cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
			}
			catch (std::exception &e)
			{
				cout << "<" << e.what() << ">" << endl;
			}
		}

		//相机停止采集
        pRemoteFeatureControl->GetCommandFeature("AcquisitionStop")->Execute();
		//流层停止采集
		pStream->StopGrab();

		//关闭流
		pStream->Close();
		//关闭相机设备
		pDevice->Close();
		//关闭设备库
		IGXFactory::GetInstance().Uninit();
	}
	catch (CGalaxyException &e)
	{
		cout << "<" << e.GetErrorCode() << ">" << "<" << e.what() << ">" << endl;
	}
	catch (std::exception &e)
	{
		cout << "<" << e.what() << ">" << endl;
	}

    if ( NULL != pDecompressionBuf )
    {
        delete[] pDecompressionBuf;
        pDecompressionBuf = NULL;
    }

	cout << "<App exit!>" << endl;
	system("pause");

	return 0;
}