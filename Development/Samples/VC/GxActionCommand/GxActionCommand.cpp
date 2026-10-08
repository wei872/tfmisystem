#include "StdAfx.h"
#include "GxIAPI.h"
#include <iostream>
#include <string>
#include <vector>

using namespace std;

struct ST_DEVICE_WRAP
{
	ST_DEVICE_WRAP()
	{
		hDevice = NULL;
		strModelName = "";
		strSN = "";
	}

	GX_DEV_HANDLE hDevice;
	string strModelName;
	string strSN;
};

string GetErrorString(GX_STATUS emErrorStatus);

//打印错误信息，并关闭设备和库
#define GX_VERIFY_EXIT(emStatus, vecDevInfos) \
    if (emStatus != GX_STATUS_SUCCESS)     \
    {                                      \
        cout << GetErrorString(emStatus) << endl; \
		for (int i = 0; i < vecDevInfos.size(); i++) \
		{											\
			GXCloseDevice(vecDevInfos[i].hDevice); \
		}										\
        GXCloseLib();                      \
        printf("<App Exit!>\n");           \
        return emStatus;                   \
    }

#define GX_VERIFY_RET_BOOL(emStatus, strSN) \
	if (GX_STATUS_SUCCESS != emStatus) \
	{									\
		cout << "SN:" << strSN << " " << GetErrorString(emStatus) << endl; \
		return false; \
	}

#define GX_VERIFY_RET_STATUS(emStatus, strSN) \
	if (GX_STATUS_SUCCESS != emStatus) \
	{									\
		cout << "SN:" << strSN << " " << GetErrorString(emStatus) << endl; \
		return emStatus; \
	}


//检查是否所有相机都支持ActionCommand和ptp功能
bool CheckCamParameters(const vector<ST_DEVICE_WRAP>& vecDevInfos)
{
	cout << "check is all device support ActionCommand and PTP" << std::endl;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;

	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		GX_DEV_HANDLE hDevice = vecDevInfos[i].hDevice;
		GX_ENUM_VALUE stEnumValue;
		memset(&stEnumValue, 0, sizeof(stEnumValue));
		emStatus = GXGetEnumValue(hDevice, "GevSupportedOptionSelector", &stEnumValue);
		GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);
		
		bool bActionItemExist = false;
		bool bScheduledActionItemExist = false;
		bool bPtpItemExist = false;

		bool bActionSupport = false;
		bool bScheduledActionSupport = false;
		bool bPtpSupport = false;

		for (int j = 0; j < stEnumValue.nSupportedNum; j++)
		{
			if (strcmp(stEnumValue.nArrySupportedValue[j].strCurSymbolic, "Action") == 0)
			{
				bActionItemExist = true;
			}

			if (strcmp(stEnumValue.nArrySupportedValue[j].strCurSymbolic, "ScheduledAction") == 0)
			{
				bScheduledActionItemExist = true;
			}

			if (strcmp(stEnumValue.nArrySupportedValue[j].strCurSymbolic, "Ptp") == 0)
			{
				bPtpItemExist = true;
			}
		}

		if (bActionItemExist && bScheduledActionItemExist && bPtpItemExist)
		{
			emStatus = GXSetEnumValueByString(hDevice, "GevSupportedOptionSelector", "Action");
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);
			GXGetBoolValue(hDevice, "GevSupportedOption", &bActionSupport);
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);

			emStatus = GXSetEnumValueByString(hDevice, "GevSupportedOptionSelector", "ScheduledAction");
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);
			GXGetBoolValue(hDevice, "GevSupportedOption", &bScheduledActionSupport);
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);

			emStatus = GXSetEnumValueByString(hDevice, "GevSupportedOptionSelector", "Ptp");
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);
			GXGetBoolValue(hDevice, "GevSupportedOption", &bPtpSupport);
			GX_VERIFY_RET_BOOL(emStatus, vecDevInfos[i].strSN);

			if (bActionSupport && bScheduledActionSupport && bPtpSupport)
			{
				//当前相机支持Action ScheduledAction Ptp
			}
			else
			{
				cout << "SN:" << vecDevInfos[i].strSN << " don't support ActionCommand or PTP" << std::endl;
				return false;
			}
		}
		else
		{
			cout << "SN:" << vecDevInfos[i].strSN << " don't support ActionCommand or PTP" << std::endl;
			return false;
		}
	}

	return true;
}

//设置相机参数并开采
GX_STATUS SetCamParametersAndStartAcquisition(const vector<ST_DEVICE_WRAP>& vecDevInfos)
{
	std::cout << "setting cam ActionCommand parameters" << std::endl;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;

	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		GX_DEV_HANDLE hDevice = vecDevInfos[i].hDevice;
		//加载默认参数组
		emStatus = GXSetEnumValueByString(hDevice, "UserSetSelector", "Default");
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		emStatus = GXSetCommandValue(hDevice, "UserSetLoad");
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);

		//开启触发模式
		emStatus = GXSetEnumValueByString(hDevice, "TriggerMode", "On");
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);

		//触发源设置为Action0
		emStatus = GXSetEnumValueByString(hDevice, "TriggerSource", "Action0");
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);

		//设置相机ActionCommand参数
		emStatus = GXSetIntValue(hDevice, "ActionDeviceKey", 1);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		emStatus = GXSetIntValue(hDevice, "ActionGroupKey", 1);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		emStatus = GXSetIntValue(hDevice, "ActionGroupMask", 0xFFFFFFFF);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);

		//相机开始采集
		emStatus = GXSetCommandValue(hDevice, "AcquisitionStart");
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
	}

	std::cout << "setting success" << std::endl;
	return emStatus;
}

//演示ActionCommand命令
GX_STATUS ShowActionCommand(const vector<ST_DEVICE_WRAP>& vecDevInfos, GX_GIGE_ACTION_COMMAND_RESULT** pResultBuffer)
{
	std::cout << "demonstrate ActionCommand function" << std::endl;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	uint32_t ui32DeviceKey = 1;
	uint32_t ui32GroupKey = 1;
	uint32_t ui32GroupMask = 0xFFFFFFFF;
	string strSpecialIP = "";
	uint32_t ui32NumResult = (uint32_t)vecDevInfos.size();
	*pResultBuffer = new GX_GIGE_ACTION_COMMAND_RESULT[ui32NumResult];
	memset(*pResultBuffer, 0, sizeof(GX_GIGE_ACTION_COMMAND_RESULT) * ui32NumResult);

	//strBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
	string strIP = "255.255.255.255";

	emStatus = GXGigEIssueActionCommand(ui32DeviceKey, ui32GroupKey, ui32GroupMask
		, strIP.c_str(), strSpecialIP.c_str(), 500, &ui32NumResult, *pResultBuffer);
	if (GX_STATUS_SUCCESS != emStatus)
	{
		cout << GetErrorString(emStatus) << endl;
		return emStatus;
	}

	//打印结果
	for (uint32_t i = 0; i < ui32NumResult; i++)
	{
		std::cout << "Ack Return " << " ip:" << (*pResultBuffer)[i].strDeviceAddress
			<< " status:" << (*pResultBuffer)[i].nStatus << std::endl;
	}

	PGX_FRAME_BUFFER pFrameBuffer;
	//接收图像
	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		emStatus = GXDQBuf(vecDevInfos[i].hDevice, &pFrameBuffer, 1000);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		std::cout << "SN:" << vecDevInfos[i].strSN << " get image success,"
			<< " image status:" << ((pFrameBuffer->nStatus == GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame")
			<< std::endl;
		emStatus = GXQBuf(vecDevInfos[i].hDevice, pFrameBuffer);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
	}

	return emStatus;
}

//演示ScheduledActionCommand命令
GX_STATUS ShowScheduledActionCommand(const vector<ST_DEVICE_WRAP>& vecDevInfos, GX_GIGE_ACTION_COMMAND_RESULT** pResultBuffer)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	uint32_t ui32DeviceKey = 1;
	uint32_t ui32GroupKey = 1;
	uint32_t ui32GroupMask = 0xFFFFFFFF;
	string strSpecialIP = "";
	uint32_t ui32NumResult = (uint32_t)vecDevInfos.size();
	memset(*pResultBuffer, 0, sizeof(GX_GIGE_ACTION_COMMAND_RESULT) * ui32NumResult);

	//strBoardCastAddress 支持：广播(255.255.255.255)、子网广播(192.168.42.255)、单播(192.168.42.42)
	string strIP = "255.255.255.255";

	//设置PTP参数
	std::cout << "setting cam PTP parameters" << std::endl;

	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		GX_DEV_HANDLE hDevice = vecDevInfos[i].hDevice;
		//打开相机PTP功能
		emStatus = GXSetBoolValue(hDevice, "PtpEnable", true);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);

		//首先应该等待相机分配角色，需要时间约8s，循环读取PtpStatus，直到值为"Master"或"Slave"时，角色分配完成
		//然后进行时间校准，精度到1μs内需要时间约1~2min，循环设置Slave相机的PtpDataSetLatch，并读取PtpOffsetFromMaster，
		//即可获得Slave相对于Master的时间偏差，当PtpOffsetFromMaster的绝对值小于用户期望的时间精度，时间校准完成
		GX_ENUM_VALUE stEnumValue;
		memset(&stEnumValue, 0, sizeof(stEnumValue));
		emStatus = GXGetEnumValue(hDevice, "PtpStatus", &stEnumValue);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		string strCamPtpStatus = stEnumValue.stCurValue.strCurSymbolic;

		int i32Loops = 0;
		bool bStatusOK = (strcmp(strCamPtpStatus.c_str(), "Master") == 0) || (strcmp(strCamPtpStatus.c_str(), "Slave") == 0);

		while (!bStatusOK && i32Loops < 8)
		{
			Sleep(1000);
			i32Loops++;

			memset(&stEnumValue, 0, sizeof(stEnumValue));
			emStatus = GXGetEnumValue(hDevice, "PtpStatus", &stEnumValue);
			GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
			strCamPtpStatus = stEnumValue.stCurValue.strCurSymbolic;

			bStatusOK = (strcmp(strCamPtpStatus.c_str(), "Master") == 0) || (strcmp(strCamPtpStatus.c_str(), "Slave") == 0);
		}

		if (!bStatusOK)
		{
			cout << "PTP time calibration timeout" << endl;
			return emStatus;
		}
	}

	std::cout << "setting success" << std::endl;

	//演示ScheduledActionCommand命令
	std::cout << "demonstrate ScheduledActionCommand function" << std::endl;

	//获取相机当前时间戳，单位ns，计划5s后相机采一张图像
	emStatus = GXSetCommandValue(vecDevInfos[0].hDevice, "TimestampLatch");
	GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[0].strSN);
	GX_INT_VALUE stIntValue;
	memset(&stIntValue, 0, sizeof(stIntValue));
	emStatus = GXGetIntValue(vecDevInfos[0].hDevice, "TimestampLatchValue", &stIntValue);
	GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[0].strSN);
	int64_t i64TimeStamp = stIntValue.nCurValue + 5000000000;

	emStatus = GXGigEIssueScheduledActionCommand(ui32DeviceKey, ui32GroupKey, ui32GroupMask
		, i64TimeStamp, strIP.c_str(), strSpecialIP.c_str(), 500, &ui32NumResult, *pResultBuffer);
	if (GX_STATUS_SUCCESS != emStatus)
	{
		cout << GetErrorString(emStatus) << endl;
		return emStatus;
	}

	//等待相机执行
	Sleep(10000);

	//打印结果
	for (uint32_t i = 0; i < ui32NumResult; i++)
	{
		std::cout << "Ack Return " << " ip:" << (*pResultBuffer)[i].strDeviceAddress
			<< " status:" << (*pResultBuffer)[i].nStatus
			<< std::endl;
	}

	//接收图像
	PGX_FRAME_BUFFER pFrameBuffer;
	//接收图像
	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		emStatus = GXDQBuf(vecDevInfos[i].hDevice, &pFrameBuffer, 1000);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
		std::cout << "SN:" << vecDevInfos[i].strSN << " get image success,"
			<< " image status:" << ((pFrameBuffer->nStatus == GX_FRAME_STATUS_SUCCESS) ? "complete frame" : "incomplete frame")
			<< std::endl;
		emStatus = GXQBuf(vecDevInfos[i].hDevice, pFrameBuffer);
		GX_VERIFY_RET_STATUS(emStatus, vecDevInfos[i].strSN);
	}

	return emStatus;
}

//停采并关闭相机
void StopAcquisitionAndCloseCam(const vector<ST_DEVICE_WRAP>& vecDevInfos)
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	//停止采集关闭相机
	for (int i = 0; i < (int)vecDevInfos.size(); i++)
	{
		emStatus = GXSetCommandValue(vecDevInfos[i].hDevice, "AcquisitionStop");
		if (GX_STATUS_SUCCESS != emStatus)
		{
			cout << "SN:" << vecDevInfos[i].strSN << " " << GetErrorString(emStatus) << endl;
		}

		emStatus = GXCloseDevice(vecDevInfos[i].hDevice);
		if (GX_STATUS_SUCCESS != emStatus)
		{
			cout << "SN:" << vecDevInfos[i].strSN << " " << GetErrorString(emStatus) << endl;
		}
	}
}

int main(int argc, char* argv[])
{
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	vector<ST_DEVICE_WRAP> vecDevInfos;

	//初始化设备库
	emStatus = GXInitLib();
	if (emStatus != GX_STATUS_SUCCESS)
	{
		cout << GetErrorString(emStatus) << endl;
		return 0;
	}

	//枚举相机设备
	uint32_t ui32DeviceNum = 0;
	emStatus = GXUpdateAllDeviceListEx(GX_TL_TYPE_GEV, &ui32DeviceNum, 1000);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		cout << GetErrorString(emStatus) << endl;
		GXCloseLib();
		return 0;
	}

	//判断当前设备连接个数
	if (ui32DeviceNum < 1)
	{
		cout << "Gige device less than 1!" << endl;
		GXCloseLib();
		return 0;
	}

	cout << "open device" << endl;

	GX_DEVICE_INFO stDeviceInfo;
	for (int i = 1; i < ui32DeviceNum + 1; i++)
	{
		//通过index打开相机设备
		ST_DEVICE_WRAP stDevWrap;
		emStatus = GXOpenDeviceByIndex(i, &stDevWrap.hDevice);
		GX_VERIFY_EXIT(emStatus, vecDevInfos);

		memset(&stDeviceInfo, 0, sizeof(stDeviceInfo));
		emStatus = GXGetDeviceInfo(i, &stDeviceInfo);
		if (emStatus == GX_STATUS_SUCCESS)
		{
			stDevWrap.strModelName = (char *)stDeviceInfo.DevInfo.stGEVDevInfo.chModelName;
			stDevWrap.strSN = (char *)stDeviceInfo.DevInfo.stGEVDevInfo.chSerialNumber;
		}

		vecDevInfos.push_back(stDevWrap);
		
		cout << "<idx:" << i << "> <Model Name:" << stDevWrap.strModelName << "> <Serial Number:"
			<< stDevWrap.strSN << ">" << endl;
	}

	GX_GIGE_ACTION_COMMAND_RESULT* pResultBuffer = NULL;
	do 
	{
		//检查是否所有相机都支持ActionCommand和ptp功能
		if (!CheckCamParameters(vecDevInfos))
		{
			break;
		}

		//设置相机参数
		if (GX_STATUS_SUCCESS != SetCamParametersAndStartAcquisition(vecDevInfos))
		{
			break;
		}

		//演示ActionCommand命令
		if (GX_STATUS_SUCCESS != ShowActionCommand(vecDevInfos, &pResultBuffer))
		{
			break;
		}

		//演示ScheduledActionCommand命令
		if (GX_STATUS_SUCCESS != ShowScheduledActionCommand(vecDevInfos, &pResultBuffer))
		{
			break;
		}
	} while (false);

	//停止采集并关闭相机
	StopAcquisitionAndCloseCam(vecDevInfos);

	//关闭设备库
	GXCloseLib();

	if (NULL != pResultBuffer)
	{
		delete[] pResultBuffer;
		pResultBuffer = NULL;
	}

	cout << "<App exit!>" << endl;
	system("pause");

	return 0;
}

string GetErrorString(GX_STATUS emErrorStatus)
{
	char *error_info = NULL;
	size_t size = 0;
	GX_STATUS emStatus = GX_STATUS_SUCCESS;
	string strErrInfo;

	// 获取错误信息长度
	emStatus = GXGetLastError(&emErrorStatus, NULL, &size);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		strErrInfo = "<Error when calling GXGetLastError>";
		return strErrInfo;
	}

	// 分配错误信息buf
	error_info = new char[size];
	if (error_info == NULL)
	{
		strErrInfo = "<Failed to allocate memory>";
		return strErrInfo;
	}
	memset(error_info, 0, size);

	// 获取错误信息
	emStatus = GXGetLastError(&emErrorStatus, error_info, &size);
	if (emStatus != GX_STATUS_SUCCESS)
	{
		strErrInfo = "<Error when calling GXGetLastError>";
	}
	else
	{
		strErrInfo = error_info;
	}

	// 释放错误buf资源
	if (error_info != NULL)
	{
		delete[]error_info;
		error_info = NULL;
	}

	return strErrInfo;
}