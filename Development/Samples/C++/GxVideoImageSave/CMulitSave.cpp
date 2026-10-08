#include "CMulitSave.h"

//----------------------------------------------------------------------------------
/**
\brief   构造函数

\return  无
*/
//----------------------------------------------------------------------------------
CMulitSave::CMulitSave()
    : QObject(NULL)
    , m_i32BufNum(DEFAULT_BUF_NUM)
    , m_bSaveVideo(true)
    , m_bSaveImg(false)
    , m_qstrSavePath("")
    , m_bUnlimit(true)
    , m_bMaxFrame(false)
    , m_i32MaxFrame(DEFAULT_MAX_FRAME)
    , m_bMaxTime(false)
    , m_i32Time(DEFAULT_TIME)
    , m_i32TimeReal(DEFAULT_TIME)
    , m_qstrTimeUnit(DEFAULT_TIME_UNIT)
    , m_qstrImageType(DEFAULT_IMAGE_TYPE)
    , m_qstrCfaMethod(DEFAULT_IMAGE_CFA)
    , m_i32ImgQuality(DEFAULT_IMAGE_QUALITY)
    , m_bSaveImageFrame(true)
    , m_i32SaveImageFrame(DEFAULT_IMAGE_FRAME)
    , m_bSaveImageTime(true)
    , m_i32SaveImageTime(DEFAULT_IMAGE_TIME)
    , m_qstrVideoType(DEFAULT_VIDEO_TYPE)
    , m_i32VideoBitRate(DEFAULT_VIDEO_BIT_RATE)
    , m_qstrVideoFrameRateType(DEFAULT_VIDEO_FRAME_RATE_TYPE)
    , m_i32VideoFrameRate(DEFAULT_VIDEO_FRAME_RATE)
    , m_i64ImgNumberSaved(0)
    , m_i64ProcessNum(0)
    , m_i64DiscardNum(0)
    , m_i64SaveImgNum(0)
    , m_bStopSaveTimeOut(false)
    , m_bInterValTimeOut(false)
    , m_i32InterIndex(0)
    , m_dAcquisitionFrameRate(0.0)
    , m_i32CurrentFps(0)
    , m_pStopSaveTimer(NULL)
    , m_pInterValTimer(NULL)
    , m_i64Width(0)
    , m_i64Height(0)
    , m_i64PixelFormat(0)
    , m_strDisplayName("")
    , m_objVideoSaver(CGxVideoSaverPointer())
    , m_objRemoteFeature(CGXFeatureControlPointer())
    , m_objLocalFeature(CGXFeatureControlPointer())
{
    m_pStopSaveTimer = new QTimer(this);
    m_pInterValTimer = new QTimer(this);
    __SetupMap();

    __Connect();
}

//----------------------------------------------------------------------------------
/**
\brief   析构函数

\return  无
*/
//----------------------------------------------------------------------------------
CMulitSave::~CMulitSave()
{
}

//----------------------------------------------------------------------------------
/**
\brief   连接信号槽

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__Connect() const
{
    // 保存最大时间定时器
    QObject::connect(m_pStopSaveTimer, SIGNAL(timeout()),
        this, SLOT(__SlotTimeout()));

    // 保存图像时间间隔定时器
    QObject::connect(m_pInterValTimer, SIGNAL(timeout()),
        this, SLOT(__SlotInterValTimeout()));
}

//----------------------------------------------------------------------------------
/**
\brief   加载初始参数
\param   [in]    qstrPath    配置文件地址
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::InitParam(const QString& qstrPath)
{
    QFile objFile(qstrPath);
    if (!objFile.open(QFile::ReadOnly | QFile::Text))
    {
        // 打开配置文件失败直接返回
        return;
    }

    // 以UTF-8读取参数文件
    QTextStream objStream(&objFile);
    objStream.setCodec("UTF-8");
    QString qstr = objStream.readAll();
    objFile.close();

    QJsonParseError objJsonError;
    QJsonDocument objDoc = QJsonDocument::fromJson(qstr.toUtf8(), &objJsonError);
    if ((objJsonError.error != QJsonParseError::NoError) 
        || objDoc.isNull())
    {
        // 解析失败直接返回
        return;
    }

    // 解析初始化参数
    QJsonObject objRoot = objDoc.object();
    __SetJSONValueInt(objRoot, QString("BufferNum"), m_i32BufNum);
    __SetJSONValueQString(objRoot, QString("SavePath"), m_qstrSavePath);
    __SetJSONValueBool(objRoot, QString("Unlimit_isChecked"), m_bUnlimit);
    __SetJSONValueBool(objRoot, QString("MaxFrame_isChecked"), m_bMaxFrame);
    __SetJSONValueInt(objRoot, QString("MaxFrame"), m_i32MaxFrame);
    __SetJSONValueBool(objRoot, QString("Time_isChecked"), m_bMaxTime);
    __SetJSONValueInt(objRoot, QString("Time"), m_i32Time);
    __SetJSONValueQString(objRoot, QString("TimeUnit"), m_qstrTimeUnit);

    if(m_qstrTimeUnit == QString(TIME_UNIT_MINUTE))
    {
        m_i32TimeReal = m_i32Time * TIME_CONVERT_VALUE;
    }
    else if(m_qstrTimeUnit == QString(TIME_UNIT_HOUR))
    {
        m_i32TimeReal = m_i32Time * TIME_CONVERT_VALUE * TIME_CONVERT_VALUE;
    }
    else
    {
        m_i32TimeReal = m_i32Time;
    }

    __SetJSONValueBool(objRoot, QString("SaveImage_isChecked"), m_bSaveImg);
    __SetJSONValueQString(objRoot, QString("ImageType"), m_qstrImageType);
    __SetJSONValueQString(objRoot, QString("CfaMethod"), m_qstrCfaMethod);
    __SetJSONValueInt(objRoot, QString("ImageQuality"), m_i32ImgQuality);
    __SetJSONValueBool(objRoot, QString("SaveImageFrame_isChecked"), m_bSaveImageFrame);
    __SetJSONValueInt(objRoot, QString("SaveImageFrame"), m_i32SaveImageFrame);
    __SetJSONValueBool(objRoot, QString("SaveImageTime_isChecked"), m_bSaveImageTime);
    __SetJSONValueInt(objRoot, QString("SaveImageTime"), m_i32SaveImageTime);
    __SetJSONValueBool(objRoot, QString("SaveVideo_isChecked"), m_bSaveVideo);
    __SetJSONValueQString(objRoot, QString("VideoType"), m_qstrVideoType);
    __SetJSONValueInt(objRoot, QString("VideoBitRate"), m_i32VideoBitRate);
    __SetJSONValueQString(objRoot, QString("VideoFrameRateType"), m_qstrVideoFrameRateType);
    __SetJSONValueInt(objRoot, QString("VideoFrameRate"), m_i32VideoFrameRate);
}

//----------------------------------------------------------------------------------
/**
\brief   获取json值给int成员赋值
\param   [in]        objRoot    配置文件根节点
\param   [in]        qstrkey    键值
\param   [in\out]    i32Value   成员变量
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SetJSONValueInt(const QJsonObject& objRoot, const QString& qstrkey, int32_t& i32Value) const
{
    QJsonValue objJsonValue = objRoot.value(qstrkey);
    if (!objJsonValue.isUndefined())
    {
        i32Value = objJsonValue.toInt();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   获取json值给qstring成员赋值
\param   [in]        objRoot    配置文件根节点
\param   [in]        qstrkey    键值
\param   [in\out]    qstrValue  成员变量
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SetJSONValueQString(const QJsonObject& objRoot, const QString& qstrkey, QString& qstrValue) const
{
    QJsonValue objJsonValue = objRoot.value(qstrkey);
    if (!objJsonValue.isUndefined())
    {
        qstrValue = objJsonValue.toString();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   获取json值给bool成员赋值
\param   [in]        objRoot    配置文件根节点
\param   [in]        qstrkey    键值
\param   [in\out]    bValue     成员变量
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SetJSONValueBool(const QJsonObject& objRoot, const QString& qstrkey, bool& bValue) const
{
    QJsonValue objJsonValue = objRoot.value(qstrkey);
    if (!objJsonValue.isUndefined())
    {
        bValue = objJsonValue.toBool();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   保存当前参数
\param   [in]    qstrPath    配置文件地址
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::RecordParam(const QString& qstrPath) const
{
    QFile objFile(qstrPath);

    // 文件不存在创建json文件
    if (!objFile.open(QIODevice::WriteOnly | QIODevice::Text))
    {
        // 创建失败直接返回
        return;
    }

    QJsonObject objRoot;
    QJsonDocument objDoc;

    // 插入元素
    objRoot.insert("BufferNum", m_i32BufNum);
    objRoot.insert("SavePath", m_qstrSavePath);
    objRoot.insert("Unlimit_isChecked", m_bUnlimit);
    objRoot.insert("MaxFrame_isChecked", m_bMaxFrame);
    objRoot.insert("MaxFrame", m_i32MaxFrame);
    objRoot.insert("Time_isChecked", m_bMaxTime);
    objRoot.insert("Time", m_i32Time);
    objRoot.insert("TimeUnit", m_qstrTimeUnit);
    objRoot.insert("SaveImage_isChecked", m_bSaveImg);
    objRoot.insert("ImageType", m_qstrImageType);
    objRoot.insert("CfaMethod", m_qstrCfaMethod);
    objRoot.insert("ImageQuality", m_i32ImgQuality);
    objRoot.insert("SaveImageFrame_isChecked", m_bSaveImageFrame);
    objRoot.insert("SaveImageFrame", m_i32SaveImageFrame);
    objRoot.insert("SaveImageTime_isChecked", m_bSaveImageTime);
    objRoot.insert("SaveImageTime", m_i32SaveImageTime);
    objRoot.insert("SaveVideo_isChecked", m_bSaveVideo);
    objRoot.insert("VideoType", m_qstrVideoType);
    objRoot.insert("VideoBitRate", m_i32VideoBitRate);
    objRoot.insert("VideoFrameRateType", m_qstrVideoFrameRateType);
    objRoot.insert("VideoFrameRate", m_i32VideoFrameRate);
    objDoc.setObject(objRoot);

    QTextStream objOut(&objFile);
    objOut.setCodec("UTF-8");
    objOut << objDoc.toJson();
    objFile.close();

    return;
}

//----------------------------------------------------------------------------------
/**
\brief   设置采集buffer数量
\param   [in]    i32BufNum    采集buffer数量
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetBufNum(const int32_t& i32BufNum)
{
    m_i32BufNum = i32BufNum;
}

//----------------------------------------------------------------------------------
/**
\brief   获取采集buffer数量
   
\return  int32_t    采集buffer数量
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetBufNum() const
{
    return m_i32BufNum;
}

//----------------------------------------------------------------------------------
/**
\brief   设置是否录像
\param   [in]    bSaveVideo    是否录像
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveVideo(const bool& bSaveVideo)
{
    m_bSaveVideo = bSaveVideo;
}

//----------------------------------------------------------------------------------
/**
\brief   获取是否录像

\return  bool    true选中录像/false未选中录像
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetSaveVideo() const
{
    return m_bSaveVideo;
}

//----------------------------------------------------------------------------------
/**
\brief   设置是否存图
\param   [in]    bSaveImg    是否存图
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveImg(const bool& bSaveImg)
{
    m_bSaveImg = bSaveImg;
}

//----------------------------------------------------------------------------------
/**
\brief   获取是否存图

\return  bool    true选中存图/false未选中存图
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetSaveImg() const
{
    return m_bSaveImg;
}

//----------------------------------------------------------------------------------
/**
\brief   设置保存路径
\param   [in]    qstrSaveImg    保存路径
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSavePath(const QString& qstrSaveImg)
{
    m_qstrSavePath = qstrSaveImg;
}

//----------------------------------------------------------------------------------
/**
\brief   获取保存路径
\param   
\return  QString    保存路径
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetSavePath() const
{
    return m_qstrSavePath;
}

//----------------------------------------------------------------------------------
/**
\brief   设置无限制
\param   [in]    bUnlimit    是否无限制
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetUnlimit(const bool& bUnlimit)
{
    m_bUnlimit = bUnlimit;
}

//----------------------------------------------------------------------------------
/**
\brief   获取无限制
\param   
\return  bool    true选择无限制/false未选择无限制
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetUnlimit() const
{
    return m_bUnlimit;
}

//----------------------------------------------------------------------------------
/**
\brief   设置最大保存帧数
\param   [in]     bMaxFrame    是否选中最大保存帧数
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetMaxFrame(const bool& bMaxFrame)
{
    m_bMaxFrame = bMaxFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   获取最大保存帧数

\return  bool    true选择最大保存帧数/false未选择最大保存帧数
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetMaxFrame() const
{
    return m_bMaxFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   设置最大保存帧数值
\param   [in]    i32MaxFrame    最大保存帧数值
\return
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetMaxFrameVal(const int32_t& i32MaxFrame)
{
    m_i32MaxFrame = i32MaxFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   获取最大保存帧数值
 
\return  int32_t    最大保存帧数值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetMaxFrameVal() const
{
    return m_i32MaxFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   设置是否选中最大保存时间
\param   bool    最大保存时间
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetTime(const bool& bTime)
{
    m_bMaxTime = bTime;
}

//----------------------------------------------------------------------------------
/**
\brief   获取是否选中最大保存时间

\return  bool    true选中最大保存时间/false未选中最大保存时间
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetTime() const
{
    return m_bMaxTime;
}

//----------------------------------------------------------------------------------
/**
\brief   设置最大保存时间值
\param   [in]    i32Time    最大保存时间值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetTimeVal(const int32_t& i32Time)
{
    m_i32Time = i32Time;
}

//----------------------------------------------------------------------------------
/**
\brief   返回最大保存时间值

\return  int32_t    最大保存时间值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetTimeVal() const
{
    return m_i32Time;
}

//----------------------------------------------------------------------------------
/**
\brief   设置最大保存时间单位
\param   [in]    qstrTimeUnit    时间单位
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetTimeUnit(const QString& qstrTimeUnit)
{
    m_qstrTimeUnit = qstrTimeUnit;
}

//----------------------------------------------------------------------------------
/**
\brief   获取最大保存时间单位

\return  QString    最大保存时间单位
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetTimeUnit() const
{
    return m_qstrTimeUnit;
}

//----------------------------------------------------------------------------------
/**
\brief   设置最大保存时间实际值
\param   [in]    i32TimeReal    最大保存时间实际值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetTimeRealVal(const int32_t& i32TimeReal)
{
    m_i32TimeReal = i32TimeReal;
}

//----------------------------------------------------------------------------------
/**
\brief   获取最大保存时间实际值

\return  int32_t    最大保存时间实际值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetTimeRealVal() const
{
    return m_i32TimeReal;
}

//----------------------------------------------------------------------------------
/**
\brief   设置保存图像类型
\param   [in] qstrImageType    保存图像类型
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetImageType(const QString& qstrImageType)
{
    m_qstrImageType = qstrImageType;
}

//----------------------------------------------------------------------------------
/**
\brief   获取保存图像类型

\return  QString    保存图像类型
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetImageType() const
{
    return m_qstrImageType;
}

//----------------------------------------------------------------------------------
/**
\brief   设置插值方式
\param   [in]    qstrCfaMethod    插值方式
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetCfaMethod(const QString& qstrCfaMethod)
{
    m_qstrCfaMethod = qstrCfaMethod;
}

//----------------------------------------------------------------------------------
/**
\brief   获取插值方式

\return  QString    插值方式
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetCfaMethod() const
{
    return m_qstrCfaMethod;
}

//----------------------------------------------------------------------------------
/**
\brief   设置图像质量
\param   [in]    i32ImageQuality    图像质量
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetImageQuality(const int32_t& i32ImageQuality)
{
    m_i32ImgQuality = i32ImageQuality;
}

//----------------------------------------------------------------------------------
/**
\brief   获取图像质量

\return  int32_t    图像质量
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetImageQuality() const
{
    return m_i32ImgQuality;
}

//----------------------------------------------------------------------------------
/**
\brief   设置是否选中按帧间隔保存图像
\param   [in]    bSaveImageFrame    是否选中按帧间隔保存图像
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveImageFrame(const bool& bSaveImageFrame)
{
    m_bSaveImageFrame = bSaveImageFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   获取是否选中按帧间隔保存图像
\param   
\return  bool    true选中按帧间隔保存图像/false未选中按帧间隔保存图像
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetSaveImageFrame() const
{
    return m_bSaveImageFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   设置帧间隔值
\param   [in]    int32_t    帧间隔值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveImageFrameVal(const int32_t& i32SaveImageFrame)
{
    m_i32SaveImageFrame = i32SaveImageFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   获取帧间隔值

\return  int32_t    帧间隔值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetSaveImageFrameVal() const
{
    return m_i32SaveImageFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   设置是否选中按时间间隔保存图像
\param   [in]    bSaveImageTime    是否选中按时间间隔保存图像
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveImageTime(const bool& bSaveImageTime)
{
    m_bSaveImageTime = bSaveImageTime;
}

//----------------------------------------------------------------------------------
/**
\brief   获取是否选中按时间间隔保存图像
\param   
\return  bool    true选中按时间间隔保存图像/false未选中按时间间隔保存图像
*/
//----------------------------------------------------------------------------------
bool CMulitSave::GetSaveImageTime() const
{
    return m_bSaveImageTime;
}

//----------------------------------------------------------------------------------
/**
\brief   设置保存图像时间间隔值
\param   [in]    i32SaveImageTime    保存图像时间间隔值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetSaveImageTimeVal(const int32_t& i32SaveImageTime)
{
    m_i32SaveImageTime = i32SaveImageTime;
}

//----------------------------------------------------------------------------------
/**
\brief   获取保存图像时间间隔值

\return  int32_t    保存图像时间间隔值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetSaveImageTimeVal() const
{
    return m_i32SaveImageTime;
}

//----------------------------------------------------------------------------------
/**
\brief   设置录像格式
\param   [in]    qstrVideoType    录像格式
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetVideoType(const QString& qstrVideoType)
{
    m_qstrVideoType = qstrVideoType;
}

//----------------------------------------------------------------------------------
/**
\brief   获取录像格式

\return  QString    录像格式
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetVideoType() const
{
    return m_qstrVideoType;
}

//----------------------------------------------------------------------------------
/**
\brief   设置录像比特率值
\param   [in]    int32_t    录像比特率值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetVideoBitRateVal(const int32_t& i32VideoBitRate)
{
    m_i32VideoBitRate = i32VideoBitRate;
}

//----------------------------------------------------------------------------------
/**
\brief   获取录像比特率值

\return  int32_t    录像比特率值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetVideoBitRateVal() const
{
    return m_i32VideoBitRate;
}

//----------------------------------------------------------------------------------
/**
\brief   设置录像帧率类型
\param   [in]    qstrVideoFrameRateType    录像帧率类型
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetVideoFrameRateType(const QString& qstrVideoFrameRateType)
{
    m_qstrVideoFrameRateType = qstrVideoFrameRateType;
}

//----------------------------------------------------------------------------------
/**
\brief   获取录像帧率类型

\return  QString    录像帧率类型
*/
//----------------------------------------------------------------------------------
QString CMulitSave::GetVideoFrameRateType() const
{
    return m_qstrVideoFrameRateType;
}

//----------------------------------------------------------------------------------
/**
\brief   设置录像帧率值
\param   [in]    i32VideoFrameRate    录像帧率值
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::SetVideoFrameRate(const int32_t& i32VideoFrameRate)
{
    m_i32VideoFrameRate = i32VideoFrameRate;
}

//----------------------------------------------------------------------------------
/**
\brief   获取录像帧率值

\return  int32_t    录像帧率值
*/
//----------------------------------------------------------------------------------
int32_t CMulitSave::GetVideoFrameRate() const
{
    return m_i32VideoFrameRate;
}

//----------------------------------------------------------------------------------
/**
\brief   初始化录像存图参数
\param   [in]    objLocalDevFeature    本地属性控制器
\param   [in]    objRemoteDevFeature   远端属性控制器
\param   [in]    objStreamDevFeature   流属性控制器
\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::InitStartParam(const CGXFeatureControlPointer& objLocalDevFeature
    , const CGXFeatureControlPointer& objRemoteDevFeature
    , const CGXFeatureControlPointer& objStreamDevFeature)
{
    m_objRemoteFeature = objRemoteDevFeature;
    m_objLocalFeature = objLocalDevFeature;
    m_objStreamFeature = objStreamDevFeature;

    m_i64ProcessNum = 0;
    m_i64ImgNumberSaved = 0;
    m_i32InterIndex = 0;
    m_bStopSaveTimeOut = false;
    m_i64DiscardNum = 0;

    m_i64Width = 0;
    m_i64Height = 0;
    m_i64PixelFormat = 0;
    GX_ENUM_ITEM_VALUE stPixelFormat;

    // 获取图像宽
    bool bSupport = m_objLocalFeature->IsImplemented("OutputWidth");
    if (bSupport)
    {
        m_i64Width = m_objLocalFeature->GetIntFeature("OutputWidth")->GetValue();
    }
    else
    {
        m_i64Width = m_objRemoteFeature->GetIntFeature("Width")->GetValue();
    }

    // 获取图像高
    bSupport = m_objLocalFeature->IsImplemented("OutputHeight");
    if (bSupport)
    {
        m_i64Height = m_objLocalFeature->GetIntFeature("OutputHeight")->GetValue();
    }
    else
    {
        m_i64Height = m_objRemoteFeature->GetIntFeature("Height")->GetValue();
    }

    // 获取图像像素格式
    bSupport = m_objLocalFeature->IsImplemented("OutPixelFormat");
    if (bSupport)
    {
        stPixelFormat = m_objLocalFeature->GetEnumFeature("OutPixelFormat")->GetEnumValue();
        m_i64PixelFormat = stPixelFormat.nCurValue;
    }
    else
    {
        stPixelFormat = m_objRemoteFeature->GetEnumFeature("PixelFormat")->GetEnumValue();
        m_i64PixelFormat = stPixelFormat.nCurValue;
    }

    // 获取相机采集帧率
    m_dAcquisitionFrameRate = m_objRemoteFeature->GetFloatFeature("CurrentAcquisitionFrameRate")->GetValue();

    // 获取工具帧率
    if (m_qstrVideoFrameRateType == QString(VIDEO_FRAMERATE_TYPE_ORI))
    {
        m_i32CurrentFps = (int32_t)m_dAcquisitionFrameRate;
    }
    else
    {
        m_i32CurrentFps = m_i32VideoFrameRate;
    }

    // 获取相机显示名称
    gxstring gxstrDisplayName = m_objLocalFeature->GetStringFeature("DeviceDisplayName")->GetValue();
    m_strDisplayName = gxstrDisplayName.c_str();

    if (!m_bSaveImg)
    {
        // 录像
        // 创建录像句柄
        GX_RECORD_PARAM stRecorder;
        stRecorder.emPixelFormat = (GX_PIXEL_FORMAT_ENTRY)m_i64PixelFormat;
        if (m_qstrVideoType == QString(VIDEO_TYPE_AVI))
        {
            stRecorder.emVideoFormat = GX_VIDEO_FORMAT_H264_AVI;
        }
        else if (m_qstrVideoType == QString(VIDEO_TYPE_MP4))
        {
            stRecorder.emVideoFormat = GX_VIDEO_FORMAT_H264_MP4;
        }
        else if (m_qstrVideoType == QString(VIDEO_TYPE_ORI_AVI))
        {
            stRecorder.emVideoFormat = GX_VIDEO_FORMAT_ORIGINAL_AVI;
        }
        else
        {
        }

        stRecorder.nWidth = (uint32_t)m_i64Width;
        stRecorder.nHeight = (uint32_t)m_i64Height;
        stRecorder.nFrameRate = (uint32_t)m_i32CurrentFps;
        stRecorder.nBitRate = (uint32_t)m_i32VideoBitRate;
        std::string strPathName = GetSaveName();
        stRecorder.pPathName = (char*)strPathName.c_str();
        m_objVideoSaver = IGXFactory::GetInstance().CreateVideoSaver(&stRecorder);
    }
}

//----------------------------------------------------------------------------------
/**
\brief   开启定时器

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::StartTimer()
{
    if (m_bSaveImg)
    {
        // 存图
        // 判断是否为间隔时长保存模式，若是开启间隔定时器
        if (m_bSaveImageTime)
        {
            m_pInterValTimer->start(m_i32SaveImageTime);
        }
    }

    // 定时模式开启定时器
    if (m_bMaxTime)
    {
        m_pStopSaveTimer->start(m_i32TimeReal * TIME_MS_S_CONVERT_VALUE);
    }
}

//----------------------------------------------------------------------------------
/**
\brief   关闭定时器

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::StopTimer()
{
    if (m_bMaxTime)
    {
        m_bStopSaveTimeOut = false;

        // 定时模式，关闭定时器
        m_pStopSaveTimer->stop();
    }

    if (m_bSaveImg && m_bSaveImageTime)
    {
        // 保存图像的时间间隔模式，关闭定时器
        m_pInterValTimer->stop();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   释放录像存图参数

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::DestroyParam()
{
    StopRecord();
}

//----------------------------------------------------------------------------------
/**
\brief   获取保存文件名称

\return  string    保存文件名称
*/
//----------------------------------------------------------------------------------
std::string CMulitSave::GetSaveName() const
{
    std::string strSavePath = "";
    std::string strType = "";
    std::string strPrefix = "";

    if (m_bSaveVideo)
    {
        strPrefix = m_strDisplayName;
        strPrefix += "_";
        if ((m_qstrVideoType == QString(VIDEO_TYPE_AVI)) 
            || (m_qstrVideoType == QString(VIDEO_TYPE_ORI_AVI)))
        {
            strType = ".avi";
        }
        else if(m_qstrVideoType == QString(VIDEO_TYPE_MP4))
        {
            strType = ".mp4";
        }
        else
        {
        }
    }
    else
    {
        strPrefix += "Pic_";
        strType = QString("-%1").arg(m_i64SaveImgNum, 0, SCALE_OF_TEN).toStdString();
        if (m_qstrImageType == QString(IMG_TYPE_BMP))
        {
            strType += ".bmp";
        }
        else if (m_qstrImageType == QString(IMG_TYPE_JPEG))
        {
            strType += ".jpg";
        }
        else if (m_qstrImageType == QString(IMG_TYPE_PNG))
        {
            strType += ".png";
        }
        else if (m_qstrImageType == QString(IMG_TYPE_TIFF))
        {
            strType += ".tiff";
        }
        else if (m_qstrImageType == QString(IMG_TYPE_RAW))
        {
            strType += QString("_W%1_H%2_F%3")
                .arg(m_i64Width, 0, SCALE_OF_TEN)
                .arg(m_i64Height, 0, SCALE_OF_TEN)
                .arg(m_mapPixelFormatToString.at(m_i64PixelFormat).c_str()).toStdString();
            strType += ".raw";
        }
        else
        {
        }
    }

    std::string strCurTime = __GetCurTime();
   
    // SavePath + / + 前缀 + 时间 + 后缀
    strSavePath = QString("%1/%2%3%4")
        .arg(m_qstrSavePath)
        .arg(strPrefix.c_str())
        .arg(strCurTime.c_str())
        .arg(strType.c_str()).toLocal8Bit();

    return strSavePath;
}

//----------------------------------------------------------------------------------
/**
\brief   获取保存文件名称

\return  string    保存文件名称
*/
//----------------------------------------------------------------------------------
std::string CMulitSave::__GetCurTime() const
{
    // 获取当前时间
    QDateTime objCurrentDataTime = QDateTime::currentDateTime();
    QString qstrCurrentDate = objCurrentDataTime.toString("yyyyMMddhhmmsszzz");
    return qstrCurrentDate.toStdString();
}

//----------------------------------------------------------------------------------
/**
\brief   对当前图像进行录像或存图
\param   [in]    pImgData    当前图像
\return  bool    true保存成功/false保存失败
*/
//----------------------------------------------------------------------------------
bool CMulitSave::SaveImageVideoToFile(std::shared_ptr<HV_FRAME_INFO> pImgData)
{
    bool bSaveResult = true;

    // 存图或存视频
    if (m_bSaveImg)
    {
        bSaveResult = __SaveImage(pImgData);
        ++m_i64SaveImgNum;
    }
    else
    {
        bSaveResult = __SaveVideo(pImgData);
    }

    if (!bSaveResult)
    {
        QString qstrMsg = QString("%1\r\n%2\r\n%3\r\n%4\r\n")
            .arg(QObject::tr("Fail to save images/videos, the reason maybe is:"))
            .arg(QObject::tr("1.The disk doesn't have enough free memory."))
            .arg(QObject::tr("2.The folder of saving images/videos has been deleted."))
            .arg(QObject::tr("3.The Frame rate is too high."));
        throw CGalaxyException(-1, qstrMsg.toLocal8Bit().data());
    }

    return bSaveResult;
}

//----------------------------------------------------------------------------------
/**
\brief   内存保存帧数增加

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::AddProcessNum()
{
    ++m_i64ProcessNum;
}

//----------------------------------------------------------------------------------
/**
\brief   丢弃帧数增加

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::AddDiscardNum()
{
    ++m_i64DiscardNum;
}

//----------------------------------------------------------------------------------
/**
\brief   保存帧数增加

\return  无
*/
//----------------------------------------------------------------------------------
void CMulitSave::AddSaveImgNum()
{
    ++m_i64ImgNumberSaved;
}

//----------------------------------------------------------------------------------
/**
\brief   关闭记录句柄

\return  int64_t    内存保存帧数
*/
//----------------------------------------------------------------------------------
void CMulitSave::StopRecord()
{
    if (!m_objVideoSaver.IsNull())
    {
        m_objVideoSaver->Close();
        m_objVideoSaver = CGxVideoSaverPointer();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   获取内存保存帧数

\return  int64_t    内存保存帧数
*/
//----------------------------------------------------------------------------------
int64_t CMulitSave::GetProcessNum() const
{
    return m_i64ProcessNum;
}

//----------------------------------------------------------------------------------
/**
\brief   获取硬盘保存帧数

\return  int64_t    硬盘保存帧数
*/
//----------------------------------------------------------------------------------
int64_t CMulitSave::GetImgSavedNum() const
{
    return m_i64ImgNumberSaved;
}

//----------------------------------------------------------------------------------
/**
\brief   获取相机丢帧数

\return  int64_t    相机丢帧数
*/
//----------------------------------------------------------------------------------
int64_t CMulitSave::GetDiscardNum() const
{
    return m_i64DiscardNum;
}

//----------------------------------------------------------------------------------
/**
\brief   停止记录条件

\return  bool    true到达停止记录条件/false没有到达停止记录条件
*/
//----------------------------------------------------------------------------------
bool CMulitSave::StopSaveFlag()
{
    if (m_bMaxFrame)
    {
        if (m_i64ProcessNum >= m_i32MaxFrame)
        {
            // 队列buffer达到最大帧数，停止插入队列
            return true;
        }
    }
    else if (m_bMaxTime)
    {
        // 根据超时时间保存
        if (m_bStopSaveTimeOut)
        {
            // 超时时间溢出停止保存
            return true;
        }
    }
    else
    {
        // 无限制模式
    }

    return false;
}

//----------------------------------------------------------------------------------
/**
\brief   停止保存图像条件

\return  bool    true到达停止保存图像条件/false没有到达停止保存图像条件
*/
//----------------------------------------------------------------------------------
bool CMulitSave::StartSaveImage()
{
    bool bStartSave = false;

    // 判断是否进行存图
    if (m_bSaveImageTime && m_bInterValTimeOut)
    {
        // 时间间隔模式下 时间间隔溢出 可以保存
        bStartSave = true;
        m_bInterValTimeOut = false;
    }

    if (m_bSaveImageFrame)
    {
        ++m_i32InterIndex;
        if (0 == (m_i32InterIndex % m_i32SaveImageFrame))
        {
            // 帧数间隔模式下 帧数间隔溢出 可以保存
            bStartSave = true;
            m_i32InterIndex = 0;
        }
    }

    return bStartSave;
}

//----------------------------------------------------------------------------------
/**
\brief   保存图像
\param   [in]    pImgData    当前图像
\return  bool    true保存成功/false保存失败
*/
//----------------------------------------------------------------------------------
bool CMulitSave::__SaveImage(std::shared_ptr<HV_FRAME_INFO> pImgData) const
{
    bool bSuccess = true;

    GX_SAVE_IMAGE_INFO stSaveImageInfo = { 0 };
    stSaveImageInfo.pImageBuffer = (unsigned char*)pImgData->pBuffer.get();
    stSaveImageInfo.nWidth = (uint32_t)pImgData->nWidth;
    stSaveImageInfo.nHeight = (uint32_t)pImgData->nHeight;
    stSaveImageInfo.emSrcFormat = (GX_PIXEL_FORMAT_ENTRY)pImgData->nRawPixelFormat;
    if (m_qstrCfaMethod == QString(IMG_CFA_BALANCE))
    {
        stSaveImageInfo.emCfaMethod = GX_CFA_METHOD_BALANCE;
    }
    else if (m_qstrCfaMethod == QString(IMG_CFA_QUICK))
    {
        stSaveImageInfo.emCfaMethod = GX_CFA_METHOD_QUICK;
    }
    else if (m_qstrCfaMethod == QString(IMG_CFA_OPTIMAL))
    {
        stSaveImageInfo.emCfaMethod = GX_CFA_METHOD_OPTIMAL;
    }
    else
    {
    }

    if (m_qstrImageType == QString(IMG_TYPE_JPEG))
    {
        stSaveImageInfo.emImgFormat = GX_IMAGE_FORMAT_JPEG;
    }
    else if (m_qstrImageType == QString(IMG_TYPE_PNG))
    {
        stSaveImageInfo.emImgFormat = GX_IMAGE_FORMAT_PNG;
    }
    else if (m_qstrImageType == QString(IMG_TYPE_TIFF))
    {
        stSaveImageInfo.emImgFormat = GX_IMAGE_FORMAT_TIFF;
    }
    else if (m_qstrImageType == QString(IMG_TYPE_RAW))
    {
        stSaveImageInfo.emImgFormat = GX_IMAGE_FORMAT_RAW;
    }
    else if (m_qstrImageType == QString(IMG_TYPE_BMP))
    {
        stSaveImageInfo.emImgFormat = GX_IMAGE_FORMAT_BMP;
    }
    else
    {
    }

    std::string strImgPath = GetSaveName();
    stSaveImageInfo.pImgPath = (char*)strImgPath.c_str();
    stSaveImageInfo.nImgQuality = (uint32_t)m_i32ImgQuality;

    try
    {
        IGXFactory::GetInstance().SaveImage(&stSaveImageInfo);
    }
    catch (...)
    {
        bSuccess = false;
    }

    return bSuccess;
}

//----------------------------------------------------------------------------------
/**
\brief   保存视频
\param   [in]    pImgData    当前图像
\return  bool    true保存成功/false保存失败
*/
//----------------------------------------------------------------------------------
bool CMulitSave::__SaveVideo(std::shared_ptr<HV_FRAME_INFO> pImgData)
{
    bool bSuccess = true;

    try
    {
        m_objVideoSaver->AddFrame((unsigned char*)pImgData->pBuffer.get());
    }
    catch (...)
    {
        bSuccess = false;
    }

    return bSuccess;
}

//----------------------------------------------------------------------------------
/**
\brief    定时器溢出槽函数

\return   无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SlotTimeout()
{
    m_bStopSaveTimeOut = true;
}

//----------------------------------------------------------------------------------
/**
\brief    间隔时长定时器溢出处理槽函数

\return   无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SlotInterValTimeout()
{
    m_bInterValTimeOut = true;
}

//----------------------------------------------------------------------------------
/**
\brief    初始化像素格式map

\return   无
*/
//----------------------------------------------------------------------------------
void CMulitSave::__SetupMap()
{
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_R8] = "R8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_G8] = "G8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_B8] = "B8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO8] = "Mono8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO8_SIGNED] = "Mono8_Signed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO10] = "Mono10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO10_PACKED] = "GVSP_Mono10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO12] = "Mono12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO12_PACKED] = "GVSP_Mono12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO14] = "Mono14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO10_P] = "PFNC_Mono10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO12_P] = "PFNC_Mono12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO14_P] = "PFNC_Mono14_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_MONO16] = "Mono16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR8] = "BayerGR8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG8] = "BayerRG8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB8] = "BayerGB8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG8] = "BayerBG8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR10] = "BayerGR10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG10] = "BayerRG10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB10] = "BayerGB10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG10] = "BayerBG10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR10_P] = "PFNC_BayerGR10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG10_P] = "PFNC_BayerRG10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB10_P] = "PFNC_BayerGB10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG10_P] = "PFNC_BayerBG10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR12] = "BayerGR12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG12] = "BayerRG12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB12] = "BayerGB12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG12] = "BayerBG12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR12_P] = "PFNC_BayerGR12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG12_P] = "PFNC_BayerRG12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB12_P] = "PFNC_BayerGB12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG12_P] = "PFNC_BayerBG12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR14] = "BayerGR14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG14] = "BayerRG14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB14] = "BayerGB14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG14] = "BayerBG14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR14_P] = "PFNC_BayerGR14_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG14_P] = "PFNC_BayerRG14_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB14_P] = "PFNC_BayerGB14_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG14_P] = "PFNC_BayerBG14_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR16] = "BayerGR16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG16] = "BayerRG16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB16] = "BayerGB16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG16] = "BayerBG16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB8] = "RGB8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGR8] = "BGR8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGBA8] = "RGBA8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGRA8] = "BGRA8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB10] = "RGB10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGR10] = "BGR10";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB12] = "RGB12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGR12] = "BGR12";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB14] = "RGB14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGR14] = "BGR14";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB16] = "RGB16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BGR16] = "BGR16";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_YUV411_8] = "YUV411_8_UYYVYY";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_YUV422_8] = "YUV422_8";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_YUV422_8_UYVY] = "YUV422_8_UYVY";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_YUV444_8] = "YUV8_UYV";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB8_PLANAR] = "RGB8_Planar";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB10_PLANAR] = "RGB10_Planar";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB12_PLANAR] = "RGB12_Planar";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_RGB16_PLANAR] = "RGB16_Planar";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG10_PACKED] = "GVSP_BayerBG10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_BG12_PACKED] = "GVSP_BayerBG12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB10_PACKED] = "GVSP_BayerGB10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GB12_PACKED] = "GVSP_BayerGB12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR10_PACKED] = "GVSP_BayerGR10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_GR12_PACKED] = "GVSP_BayerGR12_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG10_PACKED] = "GVSP_BayerRG10_Packed";
    m_mapPixelFormatToString[GX_PIXEL_FORMAT_BAYER_RG12_PACKED] = "GVSP_BayerRG12_Packed";
}