#include "StdAfx.h"
#include "GxIAPI.h"
#include "GxPixelFormat.h"
#include "DxImageProc.h"
#include <stdlib.h>

void GetErrorString(GX_STATUS emErrorStatus);

//打印错误信息，并关闭设备和库
#define GX_VERIFY_EXIT(emStatus, hDevice) \
    if (emStatus != GX_STATUS_SUCCESS)     \
    {                                      \
        GetErrorString(emStatus);          \
        GXCloseDevice(hDevice);          \
        hDevice = NULL;                  \
        GXCloseLib();                      \
        printf("<App Exit!>\n");           \
        system("pause");                   \
        return emStatus;                   \
    }

//打印错误信息，并关闭设备和库
#define DX_VERIFY_EXIT(emStatus, hDevice) \
    if (emStatus != DX_OK)     \
    {                                      \
        GXCloseDevice(hDevice);          \
        hDevice = NULL;                  \
        GXCloseLib();                      \
        printf("<App Exit!>\n");           \
        system("pause");                   \
        return emStatus;                   \
    }

// 打印设备信息
bool PrintDeviceInfo(GX_DEV_HANDLE hDevice)
{
    printf("***********************************************\n");

    GX_DEVICE_INFO stDeviceInfo;
    GX_STATUS emStatus = GXGetDeviceInfo(1, &stDeviceInfo);
    if ( emStatus != GX_STATUS_SUCCESS )
    {
        GetErrorString(emStatus);
        return false;
    }

    switch ( stDeviceInfo.emDevType )
    {
    case GX_DEVICE_CLASS_USB2:
        printf("<Vendor Name:   %s>\n", (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chVendorName[0]));
        printf("<Model Name:    %s>\n", (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chModelName[0]));
        printf("<Serial Number: %s>\n", (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chSerialNumber[0]));
        break;
    case GX_DEVICE_CLASS_GEV:
        printf("<Vendor Name:   %s>\n", (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chVendorName[0]));
        printf("<Model Name:    %s>\n", (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chModelName[0]));
        printf("<Serial Number: %s>\n", (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chSerialNumber[0]));
        break;
    case GX_DEVICE_CLASS_U3V:
        printf("<Vendor Name:   %s>\n", (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chVendorName[0]));
        printf("<Model Name:    %s>\n", (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chModelName[0]));
        printf("<Serial Number: %s>\n", (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chSerialNumber[0]));
        break;
    case GX_DEVICE_CLASS_CXP:
        printf("<Vendor Name:   %s>\n", (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chVendorName[0]));
        printf("<Model Name:    %s>\n", (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chModelName[0]));
        printf("<Serial Number: %s>\n", (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chSerialNumber[0]));
        break;
    default:
        printf( "Not support device info!\n");
        break;
    }

    printf("***********************************************\n");

    return true;
}

int main(int argc, char* argv[])
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;

	//初始化设备库
	emStatus = GXInitLib();
	if (emStatus != GX_STATUS_SUCCESS)
	{
		GetErrorString(emStatus);

        printf("<App exit!>\n");
        system("pause");
		return 0;
	}

	//枚举相机设备
	uint32_t ui32DeviceNum = 0;
	emStatus = GXUpdateAllDeviceList(&ui32DeviceNum, 1000);
	if (emStatus != GX_STATUS_SUCCESS)
	{
	    GetErrorString(emStatus);
		GXCloseLib();

        printf("<App exit!>\n");
        system("pause");
		return 0;
	}

	//判断当前设备连接个数
	if (ui32DeviceNum <= 0)
	{
        GXCloseLib();

        printf("No device!\n");
        printf("<App exit!>\n");
        system("pause");
		return 0;
	}

	//通过index打开相机设备
	GX_DEV_HANDLE hDevice = NULL;
	emStatus = GXOpenDeviceByIndex(1, &hDevice);
	if (emStatus != GX_STATUS_SUCCESS)
	{
	    GetErrorString(emStatus);
        GXCloseLib();

        printf("<App exit!>\n");
        system("pause");
        return 0;
	}
		
	//选择默认参数组
	emStatus = GXSetEnumValueByString(hDevice, "UserSetSelector", "default");
	GX_VERIFY_EXIT(emStatus, hDevice);
	//加载参数组
	emStatus = GXSetCommandValue(hDevice, "UserSetLoad");
	GX_VERIFY_EXIT(emStatus, hDevice);

    //打印设备信息
    bool bSuccess = PrintDeviceInfo(hDevice);
    if ( !bSuccess )
    {
        GXCloseDevice(hDevice);
        GXCloseLib();

        printf("<App exit!>\n");
        system("pause");
        return 0;
    }

    //校验当前设备是否支持压缩功能
    GX_NODE_ACCESS_MODE emAccessMode = GX_NODE_ACCESS_MODE_UNDEF;
    emStatus = GXGetNodeAccessMode(hDevice, "ImageCompressionMode", &emAccessMode);
    GX_VERIFY_EXIT(emStatus, hDevice);

    if ( emAccessMode != GX_NODE_ACCESS_MODE_RW )
    {
        GXCloseLib();

        printf("This device  does not support compression function!\n");
        printf("<App exit!>\n");
        system("pause");
        return 0;
    }

    //校验当前设备是否支持无损压缩
    GX_ENUM_VALUE stEnumValue;
    emStatus = GXGetEnumValue(hDevice, "ImageCompressionMode", &stEnumValue);
    GX_VERIFY_EXIT(emStatus, hDevice);

    bool bLossless = false;
    for ( int nIndex = stEnumValue.nSupportedNum; nIndex > 0; --nIndex )
    {
        if ( strcmp(stEnumValue.nArrySupportedValue[nIndex].strCurSymbolic, "Lossless") == 0 )
        {
            bLossless = true;
            break;
        }
    }

    if ( !bLossless )
    {
        GXCloseLib();

        printf("This device does not support lossless compression function!\n");
        printf("<App exit!>\n");
        system("pause");
        return 0;
    }

    //开启无损压缩
    emStatus = GXSetEnumValueByString(hDevice, "ImageCompressionMode", "Lossless");
    GX_VERIFY_EXIT(emStatus, hDevice);

    //获取解压需要的参数
    GX_ENUM_VALUE stPixelFormat;
    memset(&stPixelFormat, 0, sizeof(GX_ENUM_VALUE));
    emStatus = GXGetEnumValue(hDevice, "PixelFormat", &stPixelFormat);
    GX_VERIFY_EXIT(emStatus, hDevice);

    GX_ENUM_VALUE stComopressionMethod;
    memset(&stComopressionMethod, 0, sizeof(GX_ENUM_VALUE));
    emStatus = GXGetEnumValue(hDevice, "ImageCompressionMethod", &stComopressionMethod);
    GX_VERIFY_EXIT(emStatus, hDevice);

    GX_INT_VALUE stWidthValue;
    memset(&stWidthValue, 0, sizeof(GX_INT_VALUE));
    emStatus = GXGetIntValue(hDevice, "Width", &stWidthValue);

    GX_INT_VALUE stHeightValue;
    memset(&stHeightValue, 0, sizeof(GX_INT_VALUE));
    emStatus = GXGetIntValue(hDevice, "Height", &stHeightValue);

    GX_INT_VALUE stPayloadSizeValue;
    memset(&stPayloadSizeValue, 0, sizeof(GX_INT_VALUE));
    emStatus = GXGetIntValue(hDevice, "PayloadSize", &stPayloadSizeValue);

    uint32_t ui32PixelFormat = stPixelFormat.stCurValue.nCurValue;
    uint32_t ui32ImgWidth = stWidthValue.nCurValue;
    uint32_t ui32ImgHeight = stHeightValue.nCurValue;
    uint32_t ui32PayloadSize = stPayloadSizeValue.nCurValue;
    uint32_t ui32CompressionMedthod = stComopressionMethod.stCurValue.nCurValue;

    //创建解压handle
    DX_DECOMPRESSION_HANDLE hDecompression = NULL;
    VxInt32 emDxStatus = DxDecompressionCreate(&hDecompression);
    DX_VERIFY_EXIT(emStatus, hDevice);

	//开启采集
	emStatus = GXSetCommandValue(hDevice, "AcquisitionStart");
	GX_VERIFY_EXIT(emStatus, hDevice);

    // 申请存储解压图像的buffer
    unsigned char * pDecompressionDstBuf = (unsigned char*) malloc(sizeof(unsigned char) * ui32PayloadSize);
    if ( NULL == pDecompressionDstBuf )
    {
        GXCloseDevice(hDevice);
        GXCloseLib();
        DxDecompressionDestroy(hDecompression);

        printf("Memory allocation failed!\n");
        printf("<App exit!>\n");
        system("pause");

        return 0;
    }

    unsigned int nImageNum = 10;
    for ( size_t nIndex = 0; nIndex < nImageNum; nIndex++ )
    {
        //零拷贝采集一帧图像
        PGX_FRAME_BUFFER pFrameBuffer = NULL;
        emStatus = GXDQBuf(hDevice, &pFrameBuffer, 1000);
        if ( GX_STATUS_SUCCESS == emStatus )
        {
            // 残帧不能进行解压
            if ( GX_FRAME_STATUS_SUCCESS == pFrameBuffer->nStatus )
            {
                // 解压
                int32_t ui32DecompressionBufSize = ui32PayloadSize;
                emDxStatus = DxDecompression(hDecompression, (void*) pFrameBuffer->pImgBuf, pFrameBuffer->nImgSize, pDecompressionDstBuf, &ui32DecompressionBufSize,
                    (GX_PIXEL_FORMAT_ENTRY) ui32PixelFormat, ui32ImgWidth, ui32ImgHeight, ui32CompressionMedthod);
                if ( DX_OK != emDxStatus )
                {
                    printf("Frame ID:%d    Decompression fail! emDxStatus:%d\n", pFrameBuffer->nFrameID, emDxStatus);
                }
                else
                {
                    printf("Frame ID:%d    Compression ratio:%.2f\n", pFrameBuffer->nFrameID, (float)pFrameBuffer->nImgSize / (float)ui32DecompressionBufSize);
                }
            }
            else
            {
                printf("Abnormal Acquisition: Exception code: %d\n", pFrameBuffer->nStatus);
            }
        }
        else
        {
            GetErrorString(emStatus);
        }

        //将采集图像buffer还回到采集系统
        emStatus = GXQBuf(hDevice, pFrameBuffer);
        if ( GX_STATUS_SUCCESS != emStatus )
        {
            GetErrorString(emStatus);
        }
    }

    if ( NULL != pDecompressionDstBuf )
    {
        free(pDecompressionDstBuf);
        pDecompressionDstBuf = NULL;
    }

    //停止采集
    GXSetCommandValue(hDevice, "AcquisitionStop");

    //释放解压handle
    DxDecompressionDestroy(hDecompression);

	//关闭相机设备
	GXCloseDevice(hDevice);

	//关闭设备库
	GXCloseLib();

    printf("<App exit!>\n");
    system("pause");

	return 0;
}

void GetErrorString(GX_STATUS emErrorStatus)
{
	char *error_info = NULL;
	size_t size = 0;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;

	// 获取错误信息长度
	emStatus = GXGetLastError(&emErrorStatus, NULL, &size);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		printf("<Error when calling GXGetLastError>\n");
		return;
	}

	// 分配错误信息buf
	error_info = new char[size];
	if (error_info == NULL)
	{
		printf("<Failed to allocate memory>\n");
		return;
	}

	// 获取错误信息
	emStatus = GXGetLastError(&emErrorStatus, error_info, &size);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		printf("<Error when calling GXGetLastError>\n");
	}
	else
	{
		printf("%s\n", error_info);
	}

	// 释放错误buf资源
	if (error_info != NULL)
	{
		delete[]error_info;
		error_info = NULL;
	}
}