// GxSequencerSample.cpp : Defines the entry point for the console application.
//
#include "stdafx.h"
#include "GalaxyIncludes.h"
#include <iostream>
#include <conio.h>
using namespace std;

//-------------------------------------------------------
/**
\brief	采集回调
*/
//-------------------------------------------------------
class CSampleCaptureEventHandler : public ICaptureEventHandler
{
public:
	void DoOnImageCaptured(CImageDataPointer& objImageDataPointer, void* pUserParam)
	{
		if (objImageDataPointer->GetStatus() != 0)
		{
			cout << "残帧" << endl;
		}
		else
		{
			cout << "完整帧" << endl;
		}
	}
};


int _tmain(int argc, _TCHAR* argv[])
{
	CGXDevicePointer			objDevicePointer;					// 设备指针
	CGXStreamPointer			objStreamPointer;					// 流指针
	CGXFeatureControlPointer	objFeatureControlPointer;			// 属性控制器
	CSampleCaptureEventHandler	objCaptureEventHandler;				// 回调对象

	IGXFactory::GetInstance().Init();
	try
	{
		// 枚举设备
		GxIAPICPP::gxdeviceinfo_vector vecDeviceInfo;
		IGXFactory::GetInstance().UpdateDeviceList(1000, vecDeviceInfo);
		if (vecDeviceInfo.size() == 0)
		{
			cout << "没有连接设备" << endl;
			system("pause");
			return 0;
		}

		// 打开第一台设备
		objDevicePointer = IGXFactory::GetInstance().OpenDeviceBySN(vecDeviceInfo[0].GetSN(), GX_ACCESS_CONTROL);
		objStreamPointer = objDevicePointer->OpenStream(0);

		// 注册采集回调
		objStreamPointer->RegisterCaptureCallback(&objCaptureEventHandler, NULL);

		// 获取远端属性控制器
		objFeatureControlPointer = objDevicePointer->GetRemoteFeatureControl();

		// 加载默认参数组
		objFeatureControlPointer->GetEnumFeature("UserSetSelector")->SetValue("Default");
		objFeatureControlPointer->GetCommandFeature("UserSetLoad")->Execute();

		/**配置序列组*/
		{
			// 关闭序列模式
			objFeatureControlPointer->GetEnumFeature("SequencerMode")->SetValue("Off");

			// 打开序列配置模式
			objFeatureControlPointer->GetEnumFeature("SequencerConfigurationMode")->SetValue("On");

			/**配置第一组序列*/
			objFeatureControlPointer->GetIntFeature("SequencerSetSelector")->SetValue(0);
			// 设置曝光时间
			objFeatureControlPointer->GetFloatFeature("ExposureTime")->SetValue(5000);
			// 设置增益
			objFeatureControlPointer->GetFloatFeature("Gain")->SetValue(2);
			// 设置Gamma
			objFeatureControlPointer->GetEnumFeature("GammaMode")->SetValue("User");
			objFeatureControlPointer->GetBoolFeature("GammaEnable")->SetValue(true);
			objFeatureControlPointer->GetFloatFeature("Gamma")->SetValue(1);
			// 保存第一组序列
			objFeatureControlPointer->GetCommandFeature("SequencerSetSave")->Execute();

			/**配置第二组序列*/
			objFeatureControlPointer->GetIntFeature("SequencerSetSelector")->SetValue(1);
			// 设置曝光时间
			objFeatureControlPointer->GetFloatFeature("ExposureTime")->SetValue(8000);
			// 设置增益
			objFeatureControlPointer->GetFloatFeature("Gain")->SetValue(5);
			// 设置Gamma
			objFeatureControlPointer->GetEnumFeature("GammaMode")->SetValue("User");
			objFeatureControlPointer->GetBoolFeature("GammaEnable")->SetValue(true);
			objFeatureControlPointer->GetFloatFeature("Gamma")->SetValue(2);
			// 保存第二组序列
			objFeatureControlPointer->GetCommandFeature("SequencerSetSave")->Execute();

			/**配置第三组序列*/
			objFeatureControlPointer->GetIntFeature("SequencerSetSelector")->SetValue(2);
			// 设置曝光时间
			objFeatureControlPointer->GetFloatFeature("ExposureTime")->SetValue(10000);
			// 设置增益
			objFeatureControlPointer->GetFloatFeature("Gain")->SetValue(10);
			// 设置Gamma
			objFeatureControlPointer->GetEnumFeature("GammaMode")->SetValue("User");
			objFeatureControlPointer->GetBoolFeature("GammaEnable")->SetValue(true);
			objFeatureControlPointer->GetFloatFeature("Gamma")->SetValue(3);
			// 保存第三组序列
			objFeatureControlPointer->GetCommandFeature("SequencerSetSave")->Execute();

			/**配置第四组序列*/
			objFeatureControlPointer->GetIntFeature("SequencerSetSelector")->SetValue(3);
			// 设置曝光时间
			objFeatureControlPointer->GetFloatFeature("ExposureTime")->SetValue(15000);
			// 设置增益
			objFeatureControlPointer->GetFloatFeature("Gain")->SetValue(14);
			// 设置Gamma
			objFeatureControlPointer->GetEnumFeature("GammaMode")->SetValue("User");
			objFeatureControlPointer->GetBoolFeature("GammaEnable")->SetValue(true);
			objFeatureControlPointer->GetFloatFeature("Gamma")->SetValue(4);
			// 保存第四组序列
			objFeatureControlPointer->GetCommandFeature("SequencerSetSave")->Execute();

			// 关闭序列配置模式
			objFeatureControlPointer->GetEnumFeature("SequencerConfigurationMode")->SetValue("Off");
		}

		// 设置触发模式为软触发
		objFeatureControlPointer->GetEnumFeature("TriggerSelector")->SetValue("FrameStart");
		objFeatureControlPointer->GetEnumFeature("TriggerSource")->SetValue("Software");
		objFeatureControlPointer->GetEnumFeature("TriggerMode")->SetValue("On");

		// 打开序列模式（打开触发模式后序列模式才可设）
		objFeatureControlPointer->GetEnumFeature("SequencerMode")->SetValue("On");

		// 开始采集
		objStreamPointer->StartGrab();
		objFeatureControlPointer->GetCommandFeature("AcquisitionStart")->Execute();

		// 软触发采集
		int32_t i32Count = 4;;
		while (i32Count--)
		{
			objFeatureControlPointer->GetCommandFeature("TriggerSoftware")->Execute();

			Sleep(1000);
		}

		// 停止采集
		objFeatureControlPointer->GetCommandFeature("AcquisitionStop")->Execute();
		objStreamPointer->StopGrab();
		objStreamPointer->UnregisterCaptureCallback();
		objDevicePointer->Close();
	}
	catch (CGalaxyException &objE)
	{
		if (!objDevicePointer.IsNull())
		{
			objDevicePointer->Close();
		}

		cout << "错误码: " << objE.GetErrorCode() << "  错误描述信息: " << objE.what() << endl;
	}

	IGXFactory::GetInstance().Uninit();
	system("pause");
	return 0;
}