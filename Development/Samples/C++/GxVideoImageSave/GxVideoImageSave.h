#pragma once

#include <QtWidgets/QMainWindow>
#include <QTranslator>
#include "ui_GxVideoImageSave.h"
#include "GalaxyIncludes.h"
#include <memory>
#include <atomic>
#include "CQueueManager.h"
#include "CMulitSave.h"
#include <QTimer>
#include <QImage>
#include <QPixmap>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QJsonParseError>
#include <QTextStream>
#include <QButtonGroup>
#include <QFileDialog>
#include <QThread>

#define  DEFAULT_MAIN_UI_WIDTH          1330   ///< 默认主窗口宽度
#define  DEFAULT_MAIN_UI_HEIGHT         870    ///< 默认主窗口高度
#define  DEFAULT_IMGVIDEO_WIDTH         600    ///< 默认录像存图窗口宽度
#define  DEFAULT_IMG_SHOW_UI_WIDTH      670    ///< 默认图像显示窗口宽度
#define  DEFAULT_IMG_SHOW_UI_HEIGHT     850    ///< 默认图像显示窗口高度
#define  DEFAULT_PROP_WIDTH             450    ///< 默认属性窗口宽度
#define  THREAD_WAIT_TIME               20     ///< 主线程触发时间20ms

class CDeviceOfflineEventHandler : public QObject, public IDeviceOfflineEventHandler
{
    Q_OBJECT

public:

    // 掉线回调
    void DoOnDeviceOfflineEvent(void* pUserParam);

signals:

    // 处理保存错误信息信号
    void SigDeviceOffline(QString strMsg);
};

class GxVideoImageSave : public QMainWindow
{
    Q_OBJECT

public:
    GxVideoImageSave(QWidget *parent = nullptr);
    ~GxVideoImageSave();

    // 采集线程
    void DoOnImageCaptured();

    // 录像线程
    void DoOnRecorderThread();

    // 图像处理线程
    void DoOnImgProcessThread();

private:

    // 初始化UI
    void __InitUI();

    // 设置保存模式按钮组
    void __SetLimitGroup();

    // 设置存图存视频按钮组
    void __SetImgVideoGroup();

    // 设置存图按钮组
    void __SetImgGroup();

    // 设置combobox默认值
    void __SetComboboxDefaultValue() const;

    // 加载语言模块
    void __LoadLanguage();

    // 加载初始化参数
    void __LoadInitParam();

    // 保存界面参数
    void __RecordParam();

    // 获取安装包语言
    int32_t __GetLanguageValue();

    // 获取配置文件地址
    QString __GetConfigFilePath(const QString& qstrFile) const;

    // 连接信号槽
    void __Connect();

    // 更新界面
    void __UpdateUI() const;

    // 判断输入图像是否位彩色
    bool __IsColor(std::shared_ptr<HV_FRAME_INFO> pFrame) const;

    // 判断是否位bayer格式
    bool __IsBayer(const int32_t& nPixelFormat) const;

    // 获取最佳像素位
    GX_VALID_BIT_LIST __GetBestValudBit(GX_PIXEL_FORMAT_ENTRY emPixelFormatEntry) const;

    // 获取默认保存路径
    QString __GetDefaultSavePath();

    // 计算实际时间
    int32_t __CalcRealTime(const int32_t& i32Time) const;

    // 远端相机停采
    void __StopRemoteDevice();

#ifdef WIN32
    // 判断系统版本是否位win7及以上
    bool __IsCurrentOSLaterWin7() const;
#endif

    // 申请内存
    void __AllocMemory();

signals:
    // 设置图像信号
    void SigSetImg(QPixmap objPixmap);

    // 停止保存信号
    void SigStopSave(bool bManualStop);

    // 显示错误信息
    void SigShowError(QString qstrMsg);

    // 显示提示信息
    void SigShowInfo(QString qstrMsg);

public slots:
    // 枚举设备
    void ClickBtn_EnumDev();

    // 打开设备
    void ClickBtn_OpenDev();

    // 关闭设备
    void ClickBtn_CloseDev();

    // 开始采集
    void ClickBtn_StartSnap();

    // 停止采集
    void ClickBtn_StopSnap();

    // 打开属性栏
    void ClickBtn_OpenProp();

    // 设置采集buffer个数
    void editingFinished_BufNum();

    // 显示图像
    void ClickCheckBox_ShowImg(bool bChecked);

    // 统计信息超时回调
    void TimeOut_RefreshStatistics();

    // 录像存图方式按钮
    void ClickBtn_Limit(int i32Idx);

    // 录像存图选择按钮
    void ClickBtn_ImgVideo(int i32Idx);

    // 存图按钮
    void ClickBtn_Img(int i32Idx);

    // 开始录像存图
    void ClickBtn_StartRecord();

    // 停止录像存图
    void ClickBtn_StopRecord();

    // 录像存图保存地址
    void ClickBtn_SavePath();

    // 录像存图保存地址
    void editingFinished_SavePath();

    // 最大帧数保存
    void editingFinished_MaxFrame();

    // 最大时间保存
    void editingFinished_Time();

    // 设置时间单位
    void IndexChange_TimeUnit(int i32Idx);

    // 设置图像格式
    void IndexChange_ImgType(int i32Idx);

    // 设置插值方式
    void IndexChange_CfaMethod(int i32Idx);

    // 设置图像质量
    void editingFinished_ImgQuality();

    // 按帧间隔保存图像
    void editingFinished_SaveImgFrame();

    // 按时间间隔保存图像
    void editingFinished_SaveImgTime();

    // 设置录像格式
    void IndexChange_VideoType(int i32Idx);

    // 设置录像比特率
    void editingFinished_VideoBitRate();

    // 设置录像帧率模式
    void IndexChange_VideoFrameRateType(int i32Idx);

    // 设置录像帧率
    void editingFinished_VideoFrameRate();

    // 关闭事件
    void closeEvent(QCloseEvent* pEvent);

    // 显示错误信息
    void ProcessShowError(QString qstrMsg);

    // 显示提示信息
    void ProcessShowInfo(QString qstrMsg);

    // 设置图像
    void SetImg(QPixmap objPixmap);

    // 停止保存
    void SlotStopSave(bool bManualStop);

private:
    Ui::GxVideoImageSaveClass      ui;                     ///< 界面
    QButtonGroup*                  m_pGroupLimit;          ///< 录像存图方式按钮组
    QButtonGroup*                  m_pGroupImgVideo;       ///< 录像存图按钮组
    QButtonGroup*                  m_pGroupImg;            ///< 存图方式按钮组
    QTranslator                    m_objTranslator;        ///< 翻译文件
    std::shared_ptr<CQueueManager> m_pQueueManager;        ///< 队列管理
    std::shared_ptr<CMulitSave>    m_pMultiSave;           ///< 录像存图
    GxIAPICPP::gxdeviceinfo_vector m_vecCurDevInfoList;    ///< 枚举设备列表
    CGXDevicePointer               m_objCam;               ///< 设备
    CGXStreamPointer               m_objStream;            ///< 流
    CGXFeatureControlPointer       m_objRemoteDevFeature;  ///< 远端属性控制
    CGXFeatureControlPointer       m_objLocalDevFeature;   ///< 本地属性控制
    CGXFeatureControlPointer       m_objStreamFeature;     ///< 流属性控制
    GX_DEVICE_OFFLINE_CALLBACK_HANDLE m_pOfflineHandler;   ///< 掉线回调句柄
    std::shared_ptr<CDeviceOfflineEventHandler> 
                                   m_pDevOfflineCB;        ///< 掉线回调
    CGXImageFormatConvertPointer   m_objConvert;           ///< 图像格式转换
    GX_WIND_HANDLE                 m_hDevPropWnd;          ///< 属性树窗口句柄
    std::atomic_bool               m_bSnap;                ///< 采集标志
    std::atomic_bool               m_bDevOpen;             ///< 设备打开标志
    std::atomic_bool               m_bShowImg;             ///< 显示图像标志
    std::atomic_bool               m_bShowImgTimeout;      ///< 显示图像超时溢出标志
    std::atomic_bool               m_bImgProc;             ///< 图像处理标志
    std::atomic_bool               m_bManualStop;          ///< 手动停止按钮
    std::atomic_bool               m_bStartSave;           ///< 开始保存标志
    std::atomic_bool               m_bThreadIsRunning;     ///< 录像线程正在工作
    std::atomic_bool               m_bBufAlloc;            ///< 完成buffer分配
    QTimer*                        m_pRefreshTimer;        ///< 统计信息定时器
    std::shared_ptr<std::thread>   m_pProducerThread;      ///< 生产线程
    std::shared_ptr<std::thread>   m_pRecorderThread;      ///< 记录线程
    std::shared_ptr<std::thread>   m_pImgThread;           ///< 图像处理线程
};
