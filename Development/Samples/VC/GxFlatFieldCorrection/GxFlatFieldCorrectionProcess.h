#include <iostream>
#include "GxIAPI.h"
#include "GxErrorList.h"
#include "DxImageProc.h"
using namespace std;
#define  GX_VERIFY_BREAK(emStatus) \
	if (emStatus != GX_STATUS_SUCCESS)\
                                     {\
	                                  break ;\
                                     }
#define  DX_VERIFY(emStatus) \
	if (emStatus != DX_OK)\
                                     {\
	                                  break ;\
									  }
typedef struct GX_FFC_PARAM
{
    int32_t                      nFFCExpectedGray;             ///< FFC expected gray value 
	int32_t                      nFFCFrameCount;               ///< FFC Frame Count
	string                       strCoefficient;               ///< FFC Coefficient
	string                       strAccuracy;                  ///< FFC Accuracy
	int32_t                      nFFCBlockSize;              ///< block size
	bool                         bFFCExpectedGray;             ///< Enable FFC expected gray value
}GX_FLAT_FIELD_CORRECTION_PARAM; 

typedef enum _FFC_TYPE
{
	FFC_UNKNOWN = -1,	                      ///< 未定义
	FFC_SOFTCAL_SOFTUSE = 0,	              ///< 第一类相机，相机本身不支持平场需通过软件实现
	FFC_SOFTCAL_DEVICEUSE = 1,                ///< 第二类相机，相机本身不能计算平场系数需要依靠软件计算（计算时仅需亮场），但可以应用平场系数。
	FFC_SOFTCAL_DEVICEUSE_3140 = 2,           ///< 第三类相机，相机本身不能计算平场系数需要依靠软件计算（计算时需要亮场，可选暗场），但可以应用平场系数。
	FFC_DEVICECAL_DEVICEUSE = 3,              ///< 第四类相机，相机本身可以计算平场系数并应用系数
}FFC_TYPE;


class IFlatFieldCorrectionProcess
{
public:
    IFlatFieldCorrectionProcess(const GX_DEV_HANDLE hDevice);
    virtual ~IFlatFieldCorrectionProcess();

    //设置平场参数
    virtual void SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam) = 0;

    //计算平场矫正系数
    virtual bool Calculate(bool bNeedDark);

    //开启平场校正开关
    void EnableFFC(bool bEnableFFC);

    //获取平场校正后的图像
    virtual GX_FRAME_DATA GetFFCImage() = 0;

	//导出平场系数
	virtual bool SaveFFC(const std::string& strFFCPath) = 0;

	//导入平场系数
	virtual bool LoadFFC(const std::string& strFFCPath) = 0;

    //创建平场对象
    static IFlatFieldCorrectionProcess* CreateFlatFieldCorrectionProcess(GX_DEV_HANDLE hDevice);

protected:
    //设置矫正精度
    void __SetBlockSize(int32_t i32BlockSize);

    //设置期望灰度值
    void __SetExpectedGray(int32_t nExpectedGray);

    //设置期望灰度值使能
    void __SetExpectedGrayEnable(bool bExpectedGrayEnable);

    //设置算法精度
    void __SetFFCAccuracy(std::string strFFCAccuracy);

    //设置融合帧数
    void __SetFrameCount(int32_t nFrameCount);

    //设置平场校正系数选择
    void __SetCoefficient(std::string strFFCCoefficient);

    //判断相机属于那种类型
    static FFC_TYPE __GetFFCType(GX_DEV_HANDLE hDevice);

	//导出平场系数
	virtual bool __SavePCFFC(const std::string& strFFCPath);

	//导入平场系数
	virtual bool __LoadPCFFC(const std::string& strFFCPath);

	//导出平场系数
	virtual bool __SaveDeviceFFC(const std::string& strFFCPath);

	//导入平场系数
	virtual bool __LoadDeviceFFC(const std::string& strFFCPath);

protected:
    GX_DEV_HANDLE                   m_hDevice;
    int32_t                         m_nBlockSize;
    int32_t                         m_nFrameCount;
    int32_t                         m_nExpectedGray;
    int32_t                         m_nFFCCoefficientSize;
    unsigned char*                  m_pFFCCoefficientBuffer;
	GX_FRAME_DATA                   m_stFrameData;
	DX_FLAT_FIELD_CORRECTION_HANDLE   m_hFlatFieldCorrection;
};

//第一类相机，相机本身不支持平场需通过软件实现
class CGXSoftCalSoftUseFFC : public IFlatFieldCorrectionProcess
{
public:
    CGXSoftCalSoftUseFFC(const GX_DEV_HANDLE hDevice);

    ~CGXSoftCalSoftUseFFC();

    //设置平场参数
    virtual void SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam);

    //获取平场校正后的图像
    virtual GX_FRAME_DATA GetFFCImage();

    //开启平场校正开关
	void EnableFFC(bool bEnableFFC);

	//导出平场系数
	virtual bool SaveFFC(const std::string& strFFCPath);

	//导入平场系数
	virtual bool LoadFFC(const std::string& strFFCPath);

private:
	bool        m_bEnableFFC;
};


//第二类相机，相机本身不能计算平场系数需要依靠软件计算
class CGXSoftCalDeviceUseFFC : public IFlatFieldCorrectionProcess
{
public:

    CGXSoftCalDeviceUseFFC(GX_DEV_HANDLE hDevice);

    ~CGXSoftCalDeviceUseFFC();

    //设置平场参数
    virtual void SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam);

    //获取平场校正后的图像
    virtual GX_FRAME_DATA GetFFCImage();

	//导出平场系数
	virtual bool SaveFFC(const std::string& strFFCPath);

	//导入平场系数
	virtual bool LoadFFC(const std::string& strFFCPath);
};


//第三类相机，相机本身可以计算平场系数并应用系数
class CGXDeviceCalDeviceUseFFC : public IFlatFieldCorrectionProcess
{
public:
    CGXDeviceCalDeviceUseFFC(const GX_DEV_HANDLE hDevice);

    ~CGXDeviceCalDeviceUseFFC();

    //设置平场参数
    virtual void SetFlatFieldCorrectionParam(GX_FFC_PARAM stFFCParam);

    //计算平场矫正系数
    virtual bool Calculate(bool bNeedDark);

    //获取平场校正后的图像
    virtual GX_FRAME_DATA GetFFCImage();

	//导出平场系数
	virtual bool SaveFFC(const std::string& strFFCPath);

	//导入平场系数
	virtual bool LoadFFC(const std::string& strFFCPath);
};