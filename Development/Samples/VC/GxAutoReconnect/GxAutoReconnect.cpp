#include "stdAfx.h"
#include "GxIAPI.h"
#include <iostream>
#include <string>

using namespace std;

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
        return emStatus;                   \
    }

void GX_STDC ReconnectCallBack(void *pUserParam)
{
	printf("The reconnect callback is triggered!\n");
}

void GX_STDC DisconnectCallBack(void *pUserParam)
{
	printf("The disconnect callback is triggered!\n");
}

int main()
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;

	//初始化设备库
	emStatus = GXInitLib();
	if (emStatus != GX_STATUS_SUCCESS)
	{
		GetErrorString(emStatus);
		return 0;
	}

	//枚举相机设备
	uint32_t ui32DeviceNum = 0;
	emStatus = GXUpdateAllDeviceList(&ui32DeviceNum, 1000);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		GetErrorString(emStatus);
		GXCloseLib();
		return 0;
	}

	//判断当前设备连接个数
	if (ui32DeviceNum <= 0)
	{
		printf("No device!");
		GXCloseLib();
		return 0;
	}

	//通过index打开相机设备
	GX_DEV_HANDLE hDevice = NULL;
	emStatus = GXOpenDeviceByIndex(1, &hDevice);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		GetErrorString(emStatus);
		GXCloseLib();
		return 0;
	}

	//选择默认参数组
	emStatus = GXSetEnumValueByString(hDevice, "UserSetSelector", "default");
	GX_VERIFY_EXIT(emStatus, hDevice);

	//加载参数组
	emStatus = GXSetCommandValue(hDevice, "UserSetLoad");
	GX_VERIFY_EXIT(emStatus, hDevice);

	//开启采集
	emStatus = GXSetCommandValue(hDevice, "AcquisitionStart");
	GX_VERIFY_EXIT(emStatus, hDevice);

	//启用自动重连
	GX_LOCAL_DEV_HANDLE hLocalDevice;
	GXGetLocalDeviceHandleFromDev(hDevice, &hLocalDevice);
	emStatus = GXSetBoolValue(hLocalDevice, "EnableAutoConnection", true);
	GX_VERIFY_EXIT(emStatus, hDevice);

	//注册重连回调
	emStatus = GXRegisterDeviceReconnectCallback(hDevice, NULL, ReconnectCallBack);
	GX_VERIFY_EXIT(emStatus, hDevice);
	//注册掉线回调
	emStatus = GXRegisterDeviceDisconnectCallback(hDevice, NULL, DisconnectCallBack);
	GX_VERIFY_EXIT(emStatus, hDevice);

	printf("请手动插拔相机触发掉线，测试完成后点击回车完成测试!\n");
	getchar();

	//停止采集
	emStatus = GXSetCommandValue(hDevice, "AcquisitionStop");
	GX_VERIFY_EXIT(emStatus, hDevice);

	//注销重连回调
	GXUnregisterDeviceReconnectCallback(hDevice);
	//注销掉线回调
	GXUnregisterDeviceDisconnectCallback(hDevice);

	//关闭相机设备
	GXCloseDevice(hDevice);

	//关闭设备库
	GXCloseLib();
	printf("<App exit!>\n");
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