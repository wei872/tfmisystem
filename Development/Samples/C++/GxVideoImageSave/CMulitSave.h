#pragma once
#include <QObject>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QJsonParseError>
#include <QTextStream>
#include <QDateTime>
#include <stdint.h>
#include <QString>
#include <QFile>
#include <QTimer>
#include <QWaitCondition>
#include <QMutex>
#include "GalaxyIncludes.h"
#include <chrono>
#include <iomanip>
#include <map>
#include "CQueueManager.h"

#define  DEFAULT_BUF_NUM               2                                     ///< 默认buf个数
#define  DEFAULT_MAX_FRAME             10000                                 ///< 默认最大保存帧数
#define  DEFAULT_TIME                  1                                     ///< 默认最大保存时间
#define  DEFAULT_TIME_UNIT             "Second"                              ///< 容忍最大保存时间单位
#define  DEFAULT_IMAGE_TYPE            "BMP"                                 ///< 默认保存图像格式
#define  DEFAULT_IMAGE_CFA             "Balance"                             ///< 默认图像插值方式
#define  DEFAULT_IMAGE_QUALITY         60                                    ///< 默认图像质量
#define  DEFAULT_IMAGE_FRAME           1                                     ///< 默认保存图像帧间隔
#define  DEFAULT_IMAGE_TIME            1                                     ///< 默认保存图像时间间隔
#define  DEFAULT_VIDEO_TYPE            "H.264 Video in AVI Container"        ///< 默认录像格式
#define  DEFAULT_VIDEO_BIT_RATE        1000                                  ///< 默认录像比特率
#define  DEFAULT_VIDEO_FRAME_RATE_TYPE "The original frame rate"             ///< 默认录像帧率类型
#define  DEFAULT_VIDEO_FRAME_RATE      25                                    ///< 默认录像帧率

#define  TIME_UNIT_SECOND              "Second"                              ///< 时间单位秒                  
#define  TIME_UNIT_MINUTE              "Minute"                              ///< 时间单位分钟
#define  TIME_UNIT_HOUR                "Hour"                                ///< 时间单位小时
#define  TIME_CONVERT_VALUE            60                                    ///< 时间转换系数
#define  TIME_MS_S_CONVERT_VALUE       1000                                  ///< 秒和毫秒转换系数
#define  IMG_TYPE_BMP                  "BMP"                                 ///< 图像格式bmp
#define  IMG_TYPE_JPEG                 "JPEG"                                ///< 图像格式jpg
#define  IMG_TYPE_PNG                  "PNG"                                 ///< 图像格式png
#define  IMG_TYPE_TIFF                 "TIFF"                                ///< 图像格式tiff
#define  IMG_TYPE_RAW                  "RAW"                                 ///< 图像格式raw
#define  IMG_CFA_QUICK                 "Quick"                               ///< 图像插值方式quick
#define  IMG_CFA_BALANCE               "Balance"                             ///< 插值方式balance
#define  IMG_CFA_OPTIMAL               "OPTIMAL"                             ///< 插值方式optimal
#define  VIDEO_TYPE_AVI                "H.264 Video in AVI Container"        ///< 录像格式avi
#define  VIDEO_TYPE_MP4                "H.264 Video in MP4 Container"        ///< 录像格式mp4
#define  VIDEO_TYPE_ORI_AVI            "Uncompressed Video in AVI Container" ///< 录像格式未压缩avi
#define  VIDEO_FRAMERATE_TYPE_ORI      "The original frame rate"             ///< 录像帧率类型原始
#define  VIDEO_FRAMERATE_TYPE_CUSTOM   "Custom frame rate"                   ///< 录像帧率类型自定义
#define  ONE_SECOND                    1000                                  ///< 1秒
#define  BUFFERUSAGE_PRECISION         2                                     ///< 默认buffer使用率精度
#define  DEFAULT_UNLIMIT_IDX           1                                     ///< 默认unlimit控件序号
#define  DEFAULT_MAXFRAME_IDX          2                                     ///< 默认maxframe控件序号
#define  DEFAULT_MAXTIME_IDX           3                                     ///< 默认maxtime控件序号
#define  DEFAULT_SAVEIMG_IDX           1                                     ///< 默认保存图片控件序号
#define  DEFAULT_SAVEVIDEO_IDX         2                                     ///< 默认保存视频控件序号
#define  DEFAULT_SAVEIMGFRAME_IDX      1                                     ///< 默认按帧数保存图片控件序号
#define  DEFAULT_SAVEIMGTIME_IDX       2                                     ///< 默认按事件保存图片控件序号
#define  DEFAULT_SHOWIMG_TIME_INTERVAL 50                                    ///< 默认时间间隔
#define  DEFAULT_SECOND_IDX            0                                     ///< 默认秒控件序号
#define  DEFAULT_MINUTE_IDX            1                                     ///< 默认分钟控件序号
#define  DEFAULT_HOUR_IDX              2                                     ///< 默认小时控件序号
#define  DEFAULT_BMP_IDX               0                                     ///< 默认bmp图像格式序号
#define  DEFAULT_JPEG_IDX              1                                     ///< 默认jpeg图像格式序号
#define  DEFAULT_PNG_IDX               2                                     ///< 默认png图像格式序号
#define  DEFAULT_TIFF_IDX              3                                     ///< 默认tiff图像格式序号
#define  DEFAULT_RAW_IDX               4                                     ///< 默认raw图像格式序号
#define  DEFAULT_QUICK_IDX             0                                     ///< 默认quick插值序号
#define  DEFAULT_BALANCE_IDX           1                                     ///< 默认balance插值序号
#define  DEFAULT_OPTIMAL_IDX           2                                     ///< 默认optimal插值序号
#define  DEFAULT_AVI_IDX               0                                     ///< 默认avi序号
#define  DEFAULT_MP4_IDX               1                                     ///< 默认mp4序号
#define  DEFAULT_ORIAVI_IDX            2                                     ///< 默认原始avi序号
#define  MIN_VEDIOBITRATE              1000                                  ///< 视频比特率最小值
#define  DEFAULT_REFRESH_TIME_INTERVAL 500                                   ///< 统计信息刷新时间间隔
#define  DEFAULT_FRAMETYPE_ORI_IDX     0                                     ///< 默认原始录像帧率模式序号
#define  DEFAULT_FRAMETYPE_CUSTOM_IDX  1                                     ///< 默认自定义录像帧率模式序号
#define  SCALE_OF_TEN                  10                                    ///< 十进制

typedef std::map<int64_t, std::string> MAP_PIXEL_FORMAT_TO_STRING;

class CMulitSave : public QObject
{
    Q_OBJECT

public:
    // 构造函数
    CMulitSave();

    // 析构函数
    ~CMulitSave();

    // 加载初始化参数
    void InitParam(const QString& qstrPath);

    // 记录当前参数
    void RecordParam(const QString& qstrPath) const;

    // 设置采集buffer数量
    void SetBufNum(const int32_t& i32BufNum);

    // 获取采集buffer数量
    int32_t GetBufNum() const;

    // 设置是否保存录像
    void SetSaveVideo(const bool& bSaveVideo);

    // 获取是否保存录像
    bool GetSaveVideo() const;

    // 设置是否保存图像
    void SetSaveImg(const bool& bSaveImg);

    // 获取是否保存图像
    bool GetSaveImg() const;

    // 设置保存路径
    void SetSavePath(const QString& qstrSaveImg);

    // 获取保存路径
    QString GetSavePath() const;

    // 设置是否无限制
    void SetUnlimit(const bool& bUnlimit);

    // 获取是否无限制
    bool GetUnlimit() const;

    // 设置是否按最大帧数保存
    void SetMaxFrame(const bool& bMaxFrame);

    // 获取是否按最大帧数保存
    bool GetMaxFrame() const;

    // 设置最大帧数值
    void SetMaxFrameVal(const int32_t& i32MaxFrame);

    // 获取最大帧数值
    int32_t GetMaxFrameVal() const;

    // 设置是否按最大时间保存
    void SetTime(const bool& bTime);

    // 获取是否按最大时间保存
    bool GetTime() const;

    // 设置最大时间值
    void SetTimeVal(const int32_t& i32Time);

    // 获取最大时间值
    int32_t GetTimeVal() const;

    // 设置时间单位
    void SetTimeUnit(const QString& qstrTimeUnit);

    // 获取时间单位
    QString GetTimeUnit() const;

    // 设置真实时间值
    void SetTimeRealVal(const int32_t& i32TimeReal);

    // 获取真实事件值
    int32_t GetTimeRealVal() const;

    // 设置图像类型
    void SetImageType(const QString& qstrImageType);

    // 获取图像类型
    QString GetImageType() const;

    // 设置图像插值方式
    void SetCfaMethod(const QString& qstrCfaMethod);

    // 获取图像插值方式
    QString GetCfaMethod() const;

    // 设置图像质量
    void SetImageQuality(const int32_t& i32ImageQuality);

    // 获取图像质量
    int32_t GetImageQuality() const;

    // 设置是否按帧数间隔保存图像
    void SetSaveImageFrame(const bool& bSaveImageFrame);

    // 获取是否按帧数间隔保存图像
    bool GetSaveImageFrame() const;

    // 设置保存图像间隔帧数
    void SetSaveImageFrameVal(const int32_t& i32SaveImageFrame);

    // 获取保存图像间隔帧数
    int32_t GetSaveImageFrameVal() const;

    // 设置是否按时间间隔保存图像
    void SetSaveImageTime(const bool& bSaveImageTime);

    // 获取是否按时间间隔保存图像 
    bool GetSaveImageTime() const;

    // 设置保存图像时间间隔
    void SetSaveImageTimeVal(const int32_t& i32SaveImageTime);

    // 获取保存图像时间间隔
    int32_t GetSaveImageTimeVal() const;

    // 设置视频类型
    void SetVideoType(const QString& qstrVideoType);

    // 获取视频类型
    QString GetVideoType() const;

    // 设置视频比特率值
    void SetVideoBitRateVal(const int32_t& i32VideoBitRate);

    // 获取视频比特率值
    int32_t GetVideoBitRateVal() const;

    // 设置视频帧率类型
    void SetVideoFrameRateType(const QString& qstrVideoFrameRateType);

    // 获取视频帧率类型
    QString GetVideoFrameRateType() const;

    // 设置视频帧率
    void SetVideoFrameRate(const int32_t& i32VideoFrameRate);

    // 获取视频帧率
    int32_t GetVideoFrameRate() const;

    // 初始化录像存图参数
    void InitStartParam(const CGXFeatureControlPointer& objLocalDevFeature
        , const CGXFeatureControlPointer& objRemoteDevFeature
        , const CGXFeatureControlPointer& objStreamDevFeature);

    // 释放录像存图参数
    void DestroyParam();

    // 获取保存文件名称
    std::string GetSaveName() const;

    // 保存图像到文件
    bool SaveImageVideoToFile(std::shared_ptr<HV_FRAME_INFO> pImgData);

    // 获取内存保存帧数
    int64_t GetProcessNum() const;

    // 获取硬盘保存帧数
    int64_t GetImgSavedNum() const;

    // 获取丢弃帧数
    int64_t GetDiscardNum() const;

    // 获取是否到达停止保存条件
    bool StopSaveFlag();

    // 获取是否到达存图条件
    bool StartSaveImage();

    // 停止记录
    void StopRecord();

    // 内存保存帧数增加
    void AddProcessNum();

    // 丢弃帧数增加
    void AddDiscardNum();

    // 保存帧数增加
    void AddSaveImgNum();

    // 开启计时器
    void StartTimer();

    // 停止计时器
    void StopTimer();

public slots:
    // 保存时长定时器溢出处理槽函数
    void __SlotTimeout();

    // 间隔时长定时器溢出处理槽函数
    void __SlotInterValTimeout();

private:
    // 获取当前时间
    std::string __GetCurTime() const;

    // 保存图像
    bool __SaveImage(std::shared_ptr<HV_FRAME_INFO> pImgData) const;

    // 保存视频
    bool __SaveVideo(std::shared_ptr<HV_FRAME_INFO> pImgData);

    // 连接信号槽
    void __Connect() const;

    // 获取json值给int成员赋值
    void __SetJSONValueInt(const QJsonObject& objRoot, const QString& qstrkey, int32_t& i32Value) const;

    // 获取json值给qstring成员赋值
    void __SetJSONValueQString(const QJsonObject& objRoot, const QString& qstrkey, QString& qstrValue) const;

    // 获取json值给bool成员赋值
    void __SetJSONValueBool(const QJsonObject& objRoot, const QString& qstrkey, bool& bValue) const;

    // 初始化像素格式map
    void __SetupMap();
    
private:
    int32_t                        m_i32BufNum;               ///< 采集帧数
    bool                           m_bSaveVideo;              ///< 是否保存视频
    bool                           m_bSaveImg;                ///< 是否保存图像
    QString                        m_qstrSavePath;            ///< 保存路径
    bool                           m_bUnlimit;                ///< 是否无限制
    bool                           m_bMaxFrame;               ///< 是否按最大帧数保存图像
    int32_t                        m_i32MaxFrame;             ///< 最大帧数
    bool                           m_bMaxTime;                ///< 是否按最大时间保存图像
    int32_t                        m_i32Time;                 ///< 保存时间
    int32_t                        m_i32TimeReal;             ///< 实际保存时间，单位:秒
    QString                        m_qstrTimeUnit;            ///< 保存时间单位
    QString                        m_qstrImageType;           ///< 图像类型
    QString                        m_qstrCfaMethod;           ///< 图像插值方式
    int32_t                        m_i32ImgQuality;           ///< 图像质量
    bool                           m_bSaveImageFrame;         ///< 是否按帧数间隔保存图像
    int32_t                        m_i32SaveImageFrame;       ///< 保存图像帧数间隔
    bool                           m_bSaveImageTime;          ///< 是否按时间间隔保存图像
    int32_t                        m_i32SaveImageTime;        ///< 保存图像时间间隔
    QString                        m_qstrVideoType;           ///< 视频类型
    int32_t                        m_i32VideoBitRate;         ///< 视频比特率
    QString                        m_qstrVideoFrameRateType;  ///< 视频帧率类型
    int32_t                        m_i32VideoFrameRate;       ///< 视频帧率
    std::atomic_int64_t            m_i64ImgNumberSaved;       ///< 已保存的图片数量
    std::atomic_int64_t            m_i64ProcessNum;           ///< 已处理的帧数
    std::atomic_int64_t            m_i64DiscardNum;           ///< 丢帧数
    std::atomic_int64_t            m_i64SaveImgNum;           ///< 已保存图片数量
    std::atomic_bool               m_bStopSaveTimeOut;        ///< 保存市场定时器时间溢出标志
    std::atomic_bool               m_bInterValTimeOut;        ///< 间隔保存时长定时器超时标志
    std::atomic_int32_t            m_i32InterIndex;           ///< 间隔帧数（用于计算是否到达间隔帧数
    double_t                       m_dAcquisitionFrameRate;   ///< 设备采集帧率
    int32_t                        m_i32CurrentFps;           ///< 当前采集帧率
    QTimer*                        m_pStopSaveTimer;          ///< 保存时长定时器
    QTimer*                        m_pInterValTimer;          ///< 间隔时长定时器(存图)
    int64_t                        m_i64Width;                ///< 图像宽
    int64_t                        m_i64Height;               ///< 图像高
    int64_t                        m_i64PixelFormat;          ///< 像素格式
    std::string                    m_strDisplayName;          ///< 相机显示名称
    CGxVideoSaverPointer           m_objVideoSaver;           ///< 保存录像指针
    CGXFeatureControlPointer       m_objRemoteFeature;        ///< 远端属性控制
    CGXFeatureControlPointer       m_objLocalFeature;         ///< 本地属性控制
    CGXFeatureControlPointer       m_objStreamFeature;        ///< 流属性控制
    MAP_PIXEL_FORMAT_TO_STRING     m_mapPixelFormatToString;  ///< 像素格式数值字符串转换
};

