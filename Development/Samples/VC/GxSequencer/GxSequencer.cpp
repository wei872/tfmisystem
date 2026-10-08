// GxSequencerSample.cpp : Defines the entry point for the console application.
//

#include "stdafx.h"
#include "GxIAPI.h"
#include <iostream>
#include <conio.h>
using namespace std;

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

#define VERIFY_STATUS_RET(emStatus) \
	if (emStatus != GX_STATUS_SUCCESS) \
	{									\
		GetErrorString(emStatus);		\
		break;							\
	}

//-------------------------------------------------------
/**
\brief	ch:采集回调函数
        en:Collection callback function
\param	[in]pFrame	ch:采集回调函数结构体，存储图像信息
                    en:Collect callback function structure and store image information
\return	void
*/
//-------------------------------------------------------
static void GX_STDC OnFrameCallbackFun(GX_FRAME_CALLBACK_PARAM* pFrame)
{
	if (GX_FRAME_STATUS_SUCCESS == pFrame->status)
	{
		cout << "完整帧" << endl;
		cout << "Full frame" << endl;
	}
	else
	{
		cout << "残帧" << endl;
		cout << "Residual frame" << endl;
	}
}

int _tmain(int argc, _TCHAR* argv[])
{
	GX_STATUS					emStatus = GX_STATUS_SUCCESS;	
	GX_OPEN_PARAM				stOpenParam;					// ch:打开参数 en:Open Parameters
	GX_DEV_HANDLE				hDevice = NULL;					// ch:设备句柄 en:device handle
	GX_FEATURE_CALLBACK_HANDLE	hEventEnd = NULL;				// ch:属性更新回调函数句柄 en:Property update callback function handle

	do
	{
		// ch:初始化
		// en:Init
		emStatus = GXInitLib();
		VERIFY_STATUS_RET(emStatus);

		// ch:子网枚举所有设备
		// en:Subnet Enumerate All Devices
		uint32_t ui32DeviceNum = 0;
		emStatus = GXUpdateAllDeviceList(&ui32DeviceNum, 1000);
		VERIFY_STATUS_RET(emStatus);

		if (ui32DeviceNum == 0)
		{
			cout << "没有连接设备" << endl;
			system("pause");
			return 0;
		}

		// ch:打开第一台设备
		// en:Turn on the first device
		stOpenParam.accessMode = GX_ACCESS_EXCLUSIVE;
		stOpenParam.openMode = GX_OPEN_INDEX;
		stOpenParam.pszContent = const_cast<char*>("1");

		emStatus = GXOpenDevice(&stOpenParam, &hDevice);
		VERIFY_STATUS_RET(emStatus);

		// ch:注册采集回调
		// en:Register Collection Callbacks
		emStatus = GXRegisterCaptureCallback(hDevice, NULL, OnFrameCallbackFun);
		VERIFY_STATUS_RET(emStatus);

		// ch:加载默认参数组
		// en:Load default parameter group
		emStatus = GXSetEnumValueByString(hDevice, "UserSetSelector", "Default");
		VERIFY_STATUS_RET(emStatus);
		emStatus = GXSetCommandValue(hDevice, "UserSetLoad");
		VERIFY_STATUS_RET(emStatus);

		// ch:配置序列组
		// en:Configure sequence groups
		{
			// ch:关闭序列模式
			// en:Turn off sequence mode
			emStatus = GXSetEnumValueByString(hDevice, "SequencerMode", "Off");
			VERIFY_STATUS_RET(emStatus);

			// ch:打开序列配置模式
			// en:Open Sequence Configuration Mode
			emStatus = GXSetEnumValueByString(hDevice, "SequencerConfigurationMode", "On");
			VERIFY_STATUS_RET(emStatus);

			// ch:配置第一组序列
			// en:Configure the first set of sequences
			emStatus = GXSetIntValue(hDevice, "SequencerSetSelector", 0);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置曝光时间
			// en:Set Eexposure Time
			emStatus = GXSetFloatValue(hDevice, "ExposureTime", 5000);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置增益
			// en:Set Gain
			emStatus = GXSetFloatValue(hDevice, "Gain", 2);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置Gamma
			// en:Set Gamma
			emStatus = GXSetEnumValueByString(hDevice, "GammaMode", "User");
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetBoolValue(hDevice, "GammaEnable", true);
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetFloatValue(hDevice, "Gamma", 1);
			VERIFY_STATUS_RET(emStatus);

			// ch:保存第一组序列
			// en:Save the first set of sequences
			emStatus = GXSetCommandValue(hDevice, "SequencerSetSave");
			VERIFY_STATUS_RET(emStatus);

			// ch:配置第二组序列
			// en:Configure the second set of sequences
			emStatus = GXSetIntValue(hDevice, "SequencerSetSelector", 1);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置曝光时间
			// en:Set Eexposure Time
			emStatus = GXSetFloatValue(hDevice, "ExposureTime", 8000);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置增益
			// en:Set Gain
			emStatus = GXSetFloatValue(hDevice, "Gain", 5);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置Gamma
			// en:Set Gamma
			emStatus = GXSetEnumValueByString(hDevice, "GammaMode", "User");
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetBoolValue(hDevice, "GammaEnable", true);
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetFloatValue(hDevice, "Gamma", 2);
			VERIFY_STATUS_RET(emStatus);

			// ch:保存第二组序列
			// en:Save the second set of sequences
			emStatus = GXSetCommandValue(hDevice, "SequencerSetSave");
			VERIFY_STATUS_RET(emStatus);

			// ch:配置第三组序列
			// en:Configure the third set of sequences
			emStatus = GXSetIntValue(hDevice, "SequencerSetSelector", 2);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置曝光时间
			// en:Set Eexposure Time
			emStatus = GXSetFloatValue(hDevice, "ExposureTime", 10000);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置增益
			// en:Set Gain
			emStatus = GXSetFloatValue(hDevice, "Gain", 10);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置Gamma
			// en:Set Gamma
			emStatus = GXSetEnumValueByString(hDevice, "GammaMode", "User");
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetBoolValue(hDevice, "GammaEnable", true);
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetFloatValue(hDevice, "Gamma", 3);
			VERIFY_STATUS_RET(emStatus);

			// ch:保存第三组序列
			// en:Save the third set of sequences
			emStatus = GXSetCommandValue(hDevice, "SequencerSetSave");
			VERIFY_STATUS_RET(emStatus);

			// ch:配置第四组序列
			// en:Configure the fourth set of sequences
			emStatus = GXSetIntValue(hDevice, "SequencerSetSelector", 3);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置曝光时间
			// en:Set Eexposure Time
			emStatus = GXSetFloatValue(hDevice, "ExposureTime", 15000);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置增益
			// en:Set Gain
			emStatus = GXSetFloatValue(hDevice, "Gain", 14);
			VERIFY_STATUS_RET(emStatus);

			// ch:设置Gamma
			// en:Set Gamma
			emStatus = GXSetEnumValueByString(hDevice, "GammaMode", "User");
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetBoolValue(hDevice, "GammaEnable", true);
			VERIFY_STATUS_RET(emStatus);
			emStatus = GXSetFloatValue(hDevice, "Gamma", 4);
			VERIFY_STATUS_RET(emStatus);

			// ch:保存第四组序列
			// en:Save the fourth set of sequences
			emStatus = GXSetCommandValue(hDevice, "SequencerSetSave");
			VERIFY_STATUS_RET(emStatus);

			// ch:关闭序列配置模式
			// en:Turn off sequence configuration mode
			emStatus = GXSetEnumValueByString(hDevice, "SequencerConfigurationMode", "Off");
			VERIFY_STATUS_RET(emStatus);
		}

		// ch:设置触发模式为软触发
		// en:Set the trigger mode to soft trigger
		emStatus = GXSetEnumValueByString(hDevice, "TriggerSelector", "FrameStart");
		VERIFY_STATUS_RET(emStatus);
		emStatus = GXSetEnumValueByString(hDevice, "TriggerSource", "Software");
		VERIFY_STATUS_RET(emStatus);
		emStatus = GXSetEnumValueByString(hDevice, "TriggerMode", "On");
		VERIFY_STATUS_RET(emStatus);

		// ch:打开序列模式（打开触发模式后序列模式才可设）
		// en:Turn on the sequence mode (the sequence mode can only be set after turning on the trigger mode)
		emStatus = GXSetEnumValueByString(hDevice, "SequencerMode", "On");
		VERIFY_STATUS_RET(emStatus);

		// ch:开始采集
		// en:Start Acquisition
		emStatus = GXSetCommandValue(hDevice, "AcquisitionStart");
		VERIFY_STATUS_RET(emStatus);

		// ch:软触发采集
		// en;Soft Trigger Acquisition
		int32_t i32Count = 4;;
		while (i32Count--)
		{
			emStatus = GXSetCommandValue(hDevice, "TriggerSoftware");
			VERIFY_STATUS_RET(emStatus);

			Sleep(1000);
		}

		// ch:停止采集
		// en:Stop Acquisition
		GXSetCommandValue(hDevice, "AcquisitionStop");
		GXUnregisterCaptureCallback(hDevice);
	} while (false);

	// ch:关闭设备
	// en:CloseDevice
	if (NULL != hDevice)
	{
		GXCloseDevice(hDevice);
		hDevice = NULL;
	}
	GXCloseLib();

	system("pause");
	return 0;
}

