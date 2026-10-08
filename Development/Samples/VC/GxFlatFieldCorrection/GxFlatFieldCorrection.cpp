#include <iostream>
#include <vector>
#include "GxFlatFieldCorrectionProcess.h"

using namespace std;

int main(int argc, char* argv[])
{
	GX_STATUS      emStatus    = GX_STATUS_SUCCESS;
	uint32_t	      ui32Num     = 0;
	GX_DEV_HANDLE  hDevice     = NULL;

	bool bInit    = false;
	bool bOpen    = false;
	bool bError   = false;
	bool bSupport = false;

	do
	{
		//初始化设备库
		emStatus = GXInitLib();
		GX_VERIFY_BREAK(emStatus);

		bInit = true;

		//枚举相机设备
		emStatus = GXUpdateAllDeviceList(&ui32Num, 1000);
		GX_VERIFY_BREAK(emStatus);

		//判断当前设备连接个数
		if (ui32Num <= 0)
		{
			printf("<NO device>\n");
			break;
		}

		//打开第一台相机设备
		GX_OPEN_PARAM  stOpenParam;
		stOpenParam.accessMode  = GX_ACCESS_EXCLUSIVE;
		stOpenParam.openMode    = GX_OPEN_INDEX;
		stOpenParam.pszContent  = "1";

		emStatus= GXOpenDevice(&stOpenParam, &hDevice);
		GX_VERIFY_BREAK(emStatus);

		bOpen = true;

		//选择默认参数组
		emStatus = GXSetEnumValueByString(hDevice, "UserSetSelector", "default");
		GX_VERIFY_BREAK(emStatus);

		//加载参数组
		emStatus = GXSetCommandValue(hDevice, "UserSetLoad");
		GX_VERIFY_BREAK(emStatus);

		printf("***********************************************\n");
		string strVendorName = "NULL";
		string strModelName = "NULL";
		string strSN = "NULL";

		GX_DEVICE_INFO stDeviceInfo;
		emStatus = GXGetDeviceInfo(1, &stDeviceInfo);
		GX_VERIFY_BREAK(emStatus);

		switch(stDeviceInfo.emDevType)
		{
		case GX_DEVICE_CLASS_USB2:
			strVendorName = (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chVendorName[0]);
			strModelName = (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chModelName[0]);
			strSN = (char *)&(stDeviceInfo.DevInfo.stUSBDevInfo.chSerialNumber[0]);
			break;
		case GX_DEVICE_CLASS_GEV:
			strVendorName = (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chVendorName[0]);
			strModelName = (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chModelName[0]);
			strSN = (char *)&(stDeviceInfo.DevInfo.stGEVDevInfo.chSerialNumber[0]);
			break;
		case GX_DEVICE_CLASS_U3V:
			strVendorName = (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chVendorName[0]);
			strModelName = (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chModelName[0]);
			strSN = (char *)&(stDeviceInfo.DevInfo.stU3VDevInfo.chSerialNumber[0]);
			break;
		case GX_DEVICE_CLASS_CXP:
			strVendorName = (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chVendorName[0]);
			strModelName = (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chModelName[0]);
			strSN = (char *)&(stDeviceInfo.DevInfo.stCXPDevInfo.chSerialNumber[0]);
			break;
		default:
			printf("Not support device info!");
			break;
		}

		printf("<Vendor Name:   %s\n",strVendorName.c_str());
		printf("<Model Name:   %s\n",strModelName.c_str());
		printf("<Serial Numbe:   %s\n",strSN.c_str());
		printf("***********************************************\n");

		//1. 创建FFC处理对象
		IFlatFieldCorrectionProcess* pFFCObj = IFlatFieldCorrectionProcess::CreateFlatFieldCorrectionProcess(hDevice);
		if (NULL == pFFCObj)
		{
			printf("<create flat field correction process error!>\n");
			bError = true;
			break;
		}

		//2. 设置平场参数
        GX_FFC_PARAM    stFFCParam;
		GX_ENUM_VALUE   stValue;
        stFFCParam.nFFCExpectedGray = 127;  //-1 ~255, -1标识用图像块儿的最大值 见说明书
        stFFCParam.nFFCFrameCount = 1;  //1,2,4,8,16 标识融合帧数

		//通过以下方法获取FFCCoefficient、FFCAccuracy、FFCBlockSize的所有支持项
		//uint32_t            nSupportedNum = 0;
		//std::vector<char*>   vecCurSymbolic;

		//emStatus = GXGetEnumValue(hDevice, "FFCCoefficient", &stEnumValue);// 根据不同的节点修改此句
		//GX_VERIFY_BREAK(emStatus);

		//nSupportedNum = stEnumValue.nSupportedNum;
		//for(int Index = 0; Index < stEnumValue.nSupportedNum; ++Index)
		//{
			//vecCurSymbolic.push_back(stEnumValue.nArrySupportedValue[Index].strCurSymbolic);
		//}

		GX_NODE_ACCESS_MODE emAccessMode;
		emStatus = GXGetNodeAccessMode(hDevice, "FFCCoefficient", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) || (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXGetEnumValue(hDevice, "FFCCoefficient",&stValue);	
			GX_VERIFY_BREAK(emStatus);
			stFFCParam.strCoefficient = string(stValue.stCurValue.strCurSymbolic);
		}

		emStatus = GXGetNodeAccessMode(hDevice, "FFCAccuracy", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) || (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXGetEnumValue(hDevice, "FFCAccuracy",&stValue);	
			GX_VERIFY_BREAK(emStatus);
			stFFCParam.strAccuracy = string(stValue.stCurValue.strCurSymbolic);
		}

		emStatus = GXGetNodeAccessMode(hDevice, "FFCBlockSize", &emAccessMode);
		GX_VERIFY_BREAK(emStatus);

		bSupport = ((emAccessMode == GX_NODE_ACCESS_MODE_RW) || (emAccessMode == GX_NODE_ACCESS_MODE_RO))
			? true : false;

		if (bSupport)
		{
			emStatus = GXGetEnumValue(hDevice, "FFCBlockSize",&stValue);	
			GX_VERIFY_BREAK(emStatus);
			stFFCParam.nFFCBlockSize = stValue.stCurValue.nCurValue;
		}

        stFFCParam.bFFCExpectedGray = true;
        pFFCObj->SetFlatFieldCorrectionParam(stFFCParam);

        //3.计算平场矫正系数 false不采集暗场， true采集暗场,
		//仅FFC_SOFTCAL_SOFTUSE与FFC_SOFTCAL_DEVICEUSE_3140类型支持
        bool bCalulate = pFFCObj->Calculate(false);
		if (!bCalulate)
		{
			printf("<Calculate flat field correction error!>\n");
			bError = true;
			break;
		}

        //4.开启平场校正开关
        bool bEnableFFC = true;
        pFFCObj->EnableFFC(bEnableFFC);

        //5.获取平场校正后的图像
        GX_FRAME_DATA stFrameData= pFFCObj->GetFFCImage();
		if (GX_FRAME_STATUS_SUCCESS == stFrameData.nStatus)
		{
			if (bEnableFFC)
			{
				printf("<App get FFC Image Success!>\n");
			}
			else
			{
				printf("<App get normal Image Success!>\n");
			}	
		}

		//6. 可选保存平场矫正系数，加载平场矫正系数
		//注意FFC_DEVICECAL_DEVICEUSE类相机 当"FFCAccuracy" 设置为PixelLevel时 保存时间较长
		//当保存路径传空时，如果相机支持则平场系数将保存到相机内部, 返回值为是否成功
		bool bSaveSuccess = pFFCObj->SaveFFC("FlatFieldCorrectionProcess.ffc");

		bool bLoadSuccess = pFFCObj->LoadFFC("FlatFieldCorrectionProcess.ffc");
        
		//关闭相机设备
		emStatus = GXCloseDevice(hDevice);
		GX_VERIFY_BREAK(emStatus);

		//关闭设备库
		emStatus = GXCloseLib();
		GX_VERIFY_BREAK(emStatus);

	}while(false);

	if(GX_STATUS_SUCCESS != emStatus || bError)
	{
		if(bOpen)
		{
			//关闭相机设备
			emStatus = GXCloseDevice(hDevice);
		}

		if(bInit)
		{
			//关闭设备库
			emStatus = GXCloseLib();
		}
	}
	printf("<App exit!>\n");
	system("pause");
	return 0;
}

