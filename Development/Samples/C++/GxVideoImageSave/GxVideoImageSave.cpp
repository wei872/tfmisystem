#include "GxVideoImageSave.h"
#include <QProcessEnvironment>
#include <QFile>
#include <QSettings>
#include <QTextCodec>
#include <QMessageBox>

#ifdef WIN32
#include <Windows.h>
#endif

//----------------------------------------------------------------------------------
/**
\brief   提示错误信息
\param   [in]    qstrMsg    错误信息
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ProcessShowError(QString qstrMsg)
{
    QMessageBox::critical(this, QObject::tr("Error"), qstrMsg);
}

//----------------------------------------------------------------------------------
/**
\brief   提示信息
\param   [in]    qstrMsg    信息
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ProcessShowInfo(QString qstrMsg)
{
    QMessageBox::information(this, QObject::tr("Infomation"), qstrMsg);
}

//----------------------------------------------------------------------------------
/**
\brief   采集线程

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::DoOnImageCaptured()
{
    CImageDataPointer pImgData = CImageDataPointer();
    bool bRet = false;
    
    while (m_bSnap)
    {
        try
        {
            // 零拷贝采集一帧图像
            pImgData = m_objStream->DQBuf(ONE_SECOND);

            // 不处理残帧
            if (pImgData->GetStatus() == GX_FRAME_STATUS_INCOMPLETE
                || pImgData.IsNull())
            {
                m_objStream->QBuf(pImgData);
                continue;
            }

            // 需要显示图像并且有超时标志时进行图像拷贝
            if (m_bShowImg && m_bShowImgTimeout)
            {
                m_pQueueManager->CopyToShow(pImgData);
                m_bShowImgTimeout = false;
            }

            // 没有开始录像或buffer没有分配完成
            if ((!m_bStartSave) || (!m_bBufAlloc))
            {
                m_objStream->QBuf(pImgData);
                continue;
            }

            bool bStopSave = m_pMultiSave->StopSaveFlag();
            if (bStopSave)
            {
                m_bStartSave = false;

                emit SigStopSave(false);

                m_objStream->QBuf(pImgData);
                continue;
            }

            if (!m_pMultiSave->GetSaveVideo())
            {
                // 存图情况
                if (m_pMultiSave->StartSaveImage())
                {
                    bRet = m_pQueueManager->CopyToConsumer(pImgData);
                    if (bRet)
                    {
                        m_pQueueManager->Notify();
                        m_pMultiSave->AddProcessNum();
                    }
                    else
                    {
                        m_pMultiSave->AddDiscardNum();
                    }
                }
            }
            else
            {
                // 存视频情况
                bRet = m_pQueueManager->CopyToConsumer(pImgData);
                if (!bRet)
                {
                    m_pMultiSave->AddDiscardNum();
                }
                else
                {
                    m_pQueueManager->Notify();
                    m_pMultiSave->AddProcessNum();
                }
            }

            m_objStream->QBuf(pImgData);
        }
        catch (CGalaxyException& e)
        {
            if (e.GetErrorCode() == GX_STATUS_TIMEOUT)
            {
                // 取图超时，继续取图
                continue;
            }
            else
            {
                QString qstrMsg = e.what();
                emit SigShowError(qstrMsg);
                break;
            }
        }
        catch (const std::bad_alloc& e)
        {
            QString qstrMsg = QObject::tr("Allocate picture/video buffer failed!");
            emit SigShowError(qstrMsg);
            break;
        }
        catch (...)
        {
            QString qstrMsg = QObject::tr("Unknown Error in CaptureThread");
            emit SigShowError(qstrMsg);
            break;
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   录像存图线程

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::DoOnRecorderThread()
{
    std::shared_ptr<HV_FRAME_INFO> pImgData = nullptr;
    bool bSaveResult = false;
    m_bThreadIsRunning = true;

    try
    {
        while (true)
        {
            int32_t nConsumerSize = m_pQueueManager->GetElemSize(CONSUMER);
            
            // 保存标志为false
            if (!m_bStartSave)
            {
                // 将消费者队列剩余内存存储完毕后退出
                if (0 == nConsumerSize)
                {
                    break;
                }

                // 手动停止录像存图，退出线程
                if (m_bManualStop)
                {
                    m_bManualStop = false;
                    break;
                }
            }

            pImgData = m_pQueueManager->PopFront(CONSUMER);
            if (nullptr == pImgData)
            {
                // 消费队列为空时，等待采集线程触发
                m_pQueueManager->WaitFor(THREAD_WAIT_TIME);
                continue;
            }

            // 进行录像存图
            bSaveResult = m_pMultiSave->SaveImageVideoToFile(pImgData);
            m_pQueueManager->PushBack(PRODUCER, pImgData);
            if (!bSaveResult)
            {
                break;
            }

            m_pMultiSave->AddSaveImgNum();
        }

        // 关闭记录句柄
        m_pMultiSave->StopRecord();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString::fromLocal8Bit(e.what()));
    }
    catch (...)
    {
        QString qstrMsg = QObject::tr("Unknown Error in RecorderThread");
        emit SigShowError(qstrMsg);
    }

    m_bThreadIsRunning = false;
}

//----------------------------------------------------------------------------------
/**
\brief   图像处理线程

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::DoOnImgProcessThread()
{
    while (m_bImgProc)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(DEFAULT_SHOWIMG_TIME_INTERVAL));

        try
        {
            if (!m_bShowImg)
            {
                continue;
            }

            // 定时器超时设置显示图像标志
            m_bShowImgTimeout = true;

            // 从显示队列获取图像
            std::shared_ptr<HV_FRAME_INFO> pFrame = m_pQueueManager->PopShowlist();
            if (pFrame == nullptr)
            {
                continue;
            }

            // 计算转换后图像大小
            int64_t i64ConvertSize = 0;
            bool bColor = false;
            bColor = __IsColor(pFrame);

            // 设置插值方式
            m_objConvert->SetInterpolationType(GX_RAW2RGB_NEIGHBOUR);

            // 设置有效位
            GX_VALID_BIT_LIST emValidBits = __GetBestValudBit(static_cast<GX_PIXEL_FORMAT_ENTRY>(pFrame->nRawPixelFormat));
            m_objConvert->SetValidBits(emValidBits);

            if (bColor)
            {
                m_objConvert->SetDstFormat(GX_PIXEL_FORMAT_RGB8);
                i64ConvertSize = m_objConvert->GetBufferSizeForConversion(pFrame->nWidth, pFrame->nHeight
                    , GX_PIXEL_FORMAT_RGB8);
            }
            else
            {
                m_objConvert->SetDstFormat(GX_PIXEL_FORMAT_MONO8);
                i64ConvertSize = m_objConvert->GetBufferSizeForConversion(pFrame->nWidth, pFrame->nHeight
                    , GX_PIXEL_FORMAT_MONO8);
            }

            // 准备显示buffer
            std::shared_ptr<uint8_t[]> pDisplayBuffer(new uint8_t[i64ConvertSize]());

            // 进行图像转换
            if (pFrame->nRawPixelFormat == GX_PIXEL_FORMAT_RGB8)
            {
                memcpy_s(pDisplayBuffer.get(), i64ConvertSize, pFrame->pBuffer.get(), pFrame->nImgBufferSize);
            }
            else
            {
                m_objConvert->Convert(pFrame->pBuffer.get()
                    , pFrame->nWidth
                    , pFrame->nHeight
                    , static_cast<GX_PIXEL_FORMAT_ENTRY>(pFrame->nRawPixelFormat)
                    , pDisplayBuffer.get(), i64ConvertSize, false);
            }

            // 显示图像
            QImage objImg;

            if (bColor)
            {
                objImg = QImage((uchar*)pDisplayBuffer.get()
                    , (int32_t)pFrame->nWidth
                    , (int32_t)pFrame->nHeight
                    , QImage::Format_RGB888);
            }
            else
            {
                objImg = QImage((uchar*)pDisplayBuffer.get()
                    , (int32_t)pFrame->nWidth
                    , (int32_t)pFrame->nHeight
                    , QImage::Format_Grayscale8);
            }

            QPixmap objPixmap = QPixmap::fromImage(objImg);
            objPixmap = objPixmap.scaled(ui.label_ImageShow->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation);
            emit SigSetImg(objPixmap);
        }
        catch (const CGalaxyException& e)
        {
            QString qstrMsg = e.what();
            emit SigShowError(qstrMsg);
            m_bImgProc = false;
            return;
        }
        catch (...)
        {
            QString qstrMsg = QObject::tr("Unknown Error in ShowImgThread");
            emit SigShowError(qstrMsg);
            m_bImgProc = false;
            return;
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   掉线回调
\param   [in]    pUserParam    用户信息
\return  无
*/
//----------------------------------------------------------------------------------
void CDeviceOfflineEventHandler::DoOnDeviceOfflineEvent(void* pUserParam)
{
    QString qstrMsg = QObject::tr("Device Offline");
    emit SigDeviceOffline(qstrMsg);
}

//----------------------------------------------------------------------------------
/**
\brief   统计信息刷新超时回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::TimeOut_RefreshStatistics()
{
    int64_t i64DiscardNum = 0;
    int64_t i64SumBufNum = 0;
    int64_t i64ProcessNum = 0;
    int64_t i64ImgSavedNum = 0;
    int64_t i64ProducerSize = 0;

    i64DiscardNum = m_pMultiSave->GetDiscardNum();
    i64SumBufNum = m_pQueueManager->AllocatedBufNum();
    i64ProcessNum = m_pMultiSave->GetProcessNum();
    i64ImgSavedNum = m_pMultiSave->GetImgSavedNum();
    i64ProducerSize = m_pQueueManager->GetElemSize(PRODUCER);

    ui.label_DiscardFramesNum->setText(QString::number(i64DiscardNum));
    ui.label_HDFramesNum->setText(QString::number(i64ImgSavedNum));
    ui.label_MemFramesNum->setText(QString::number(i64ProcessNum));
}

//----------------------------------------------------------------------------------
/**
\brief   构造函数
\param   [in]    QWidget*    父指针
\return  无
*/
//----------------------------------------------------------------------------------
GxVideoImageSave::GxVideoImageSave(QWidget *parent)
    : QMainWindow(parent)
    , m_objCam(CGXDevicePointer())
    , m_objStream(CGXStreamPointer())
    , m_objRemoteDevFeature(CGXFeatureControlPointer())
    , m_objLocalDevFeature(CGXFeatureControlPointer())
    , m_objStreamFeature(CGXFeatureControlPointer())
    , m_pOfflineHandler(NULL)
    , m_pDevOfflineCB(nullptr)
    , m_objConvert(CGXImageFormatConvertPointer())
    , m_hDevPropWnd(NULL)
    , m_pQueueManager(nullptr)
    , m_pMultiSave(nullptr)
    , m_bSnap(false)
    , m_bDevOpen(false)
    , m_bShowImg(true)
    , m_bShowImgTimeout(false)
    , m_bImgProc(false)
    , m_bManualStop(false)
    , m_bStartSave(false)
    , m_bThreadIsRunning(false)
    , m_bBufAlloc(false)
    , m_pRefreshTimer(NULL)
    , m_pProducerThread(nullptr)
    , m_pRecorderThread(nullptr)
    , m_pGroupLimit(NULL)
    , m_pGroupImgVideo(NULL)
    , m_pGroupImg(NULL)
    , m_pImgThread(nullptr)
{
    ui.setupUi(this);

    try
    {
        // 初始化c++库
        IGXFactory::GetInstance().Init();

        // 创建图像转换类
        m_objConvert = IGXFactory::GetInstance().CreateImageFormatConvert();

        // 加载语言模块
        __LoadLanguage();

        // 创建队列管理类
        m_pQueueManager = std::make_shared<CQueueManager>();

        // 创建存图存视频类
        m_pMultiSave = std::make_shared<CMulitSave>();

        // 加载初始化文件
        __LoadInitParam();

        // 界面初始化
        __InitUI();

        // 信号槽连接
        __Connect();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
        QTimer::singleShot(0, qApp, SLOT(quit()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
        QTimer::singleShot(0, qApp, SLOT(quit()));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   析构函数

\return  无
*/
//----------------------------------------------------------------------------------
GxVideoImageSave::~GxVideoImageSave()
{
    try
    {
        IGXFactory::GetInstance().Uninit();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   界面初始化

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__InitUI()
{
    int32_t i32Pos = 0;
    ui.label_ImageShow->setText("");

    // 窗口大小初始化
    this->setFixedSize(QSize(DEFAULT_MAIN_UI_WIDTH, DEFAULT_MAIN_UI_HEIGHT));
    ui.groupBox_VideoImageSave->setFixedWidth(DEFAULT_IMGVIDEO_WIDTH);
    ui.groupBox_Show->setFixedSize(QSize(DEFAULT_IMG_SHOW_UI_WIDTH, DEFAULT_IMG_SHOW_UI_HEIGHT));

    // 设备控制初始化
    ui.groupBox_DevCtrl->setTitle(QObject::tr("DeviceCtrl"));
    ui.groupBox_VideoImageSave->setTitle(QObject::tr("VideoImageSave"));
    ui.groupBox_Show->setTitle(QObject::tr("ShowImage"));
    ui.pushButton_EnumDev->setText(QObject::tr("FindDevice"));
    ui.pushButton_OpenDev->setText(QObject::tr("OpenDevice"));
    ui.pushButton_CloseDev->setText(QObject::tr("CloseDevice"));
    ui.pushButton_StartSnap->setText(QObject::tr("StartSnap"));
    ui.pushButton_StopSnap->setText(QObject::tr("StopSnap"));
    ui.pushButton_OpenProp->setText(QObject::tr("OpenProp"));
    ui.checkBox_ShowImg->setText(QObject::tr("ShowImage"));

    ui.checkBox_ShowImg->setChecked(true);
    ui.pushButton_OpenDev->setEnabled(false);
    ui.pushButton_CloseDev->setEnabled(false);
    ui.pushButton_StartSnap->setEnabled(false);
    ui.pushButton_StopSnap->setEnabled(false);
    ui.pushButton_OpenProp->setEnabled(false);

    // 录像/存图初始化
    ui.label_BasicParam->setText(QObject::tr("Basic Parameters"));
    ui.label_BufNum->setText(QObject::tr("Number of memory buffer"));
    ui.label_FileSavePath->setText(QObject::tr("File Save Path"));
    ui.label_FrameNumOrTime->setText(QObject::tr("Frames or duration limit of videos/images"));
    ui.pushButton_FileSavePath->setText(QObject::tr("..."));
    ui.checkBox_Unlimit->setText(QObject::tr("Unlimited"));
    ui.checkBox_MaxFrame->setText(QObject::tr("Maximum number of frames"));
    ui.checkBox_Time->setText(QObject::tr("Duration"));
    ui.label_SetSave->setText(QObject::tr("Save File Settings"));
    ui.checkBox_SaveImg->setText(QObject::tr("Save Image"));
    ui.label_ImgType->setText(QObject::tr("Image Format"));
    ui.label_CfaMethod->setText(QObject::tr("CFA Method"));
    ui.label_ImgQuality->setText(QObject::tr("Image Quality"));
    ui.checkBox_SaveImgFrame->setText(QObject::tr("Save every certain number of frames:"));
    ui.checkBox_SaveImgTime->setText(QObject::tr("Save at regular intervals:"));
    ui.label_Frame->setText(QObject::tr("frames"));
    ui.label_Time->setText(QObject::tr("ms"));
    ui.checkBox_SaveVideo->setText(QObject::tr("Save Video"));
    ui.label_VideoType->setText(QObject::tr("Video Format"));
    ui.label_VideoBitRate->setText(QObject::tr("Video Bit Rate(kbit/s)"));
    ui.label_VideoFrameRate->setText(QObject::tr("Broadcast frame rate(FPS)"));

    // 信息初始化
    ui.label_Msg->setText(QObject::tr("Message"));
    ui.label_HDFrames->setText(QObject::tr("Frames stored"));
    ui.label_MemFrames->setText(QObject::tr("Frames added to the consumer queue"));
    ui.label_DiscardFrames->setText(QObject::tr("Failed frames due to insufficient buffer"));
    ui.label_HDFramesNum->setText("0");
    ui.label_MemFramesNum->setText("0");
    ui.label_DiscardFramesNum->setText("0");
    ui.pushButton_Start->setText(QObject::tr("Start"));
    ui.pushButton_Stop->setText(QObject::tr("Stop"));

    QRegExpValidator* pReg_1_65535 = NULL;
    QRegExpValidator* pReg_1_50000 = NULL;
    QRegExpValidator* pReg_1_10000 = NULL;
    QRegExpValidator* pReg_1_100 = NULL;

    // 1-65535 && ""
    pReg_1_65535 = new QRegExpValidator(QRegExp(
        "^(?:"
        "[1-9]\\d{0,3}|"
        "[1-5]\\d{4}|"
        "6[0-4]\\d{3}|"
        "65[0-4]\\d{2}|"
        "655[0-2]\\d|"
        "6553[0-5]"
        ")?$"), this);

    // 1-50000 && ""
    pReg_1_50000 = new QRegExpValidator(QRegExp(
        "^(?:[1-9]\\d{0,3}|[1-4]\\d{4}|50000)?$"), this);

    // 1-10000 && ""
    pReg_1_10000 = new QRegExpValidator(QRegExp(
        "^(?:10000|[1-9][0-9]{0,3})$"), this);


    // 1-100 && ""
    pReg_1_100 = new QRegExpValidator(QRegExp(
        "^(?:[1-9]|[1-9]\\d|100)?$"), this);

    ui.lineEdit_BufNum->setValidator(pReg_1_10000);
    QString qstrBufNum = QString::number(m_pMultiSave->GetBufNum());
    if (pReg_1_10000->validate(qstrBufNum, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetBufNum(DEFAULT_BUF_NUM);
    }
    ui.lineEdit_BufNum->setText(QString::number(m_pMultiSave->GetBufNum()));

    QString qstrSavePath = m_pMultiSave->GetSavePath();
    QDir objDir(qstrSavePath);
    if (!objDir.exists(qstrSavePath))
    {
        // 初始化路径不存在时改为默认路径
        qstrSavePath = __GetDefaultSavePath();
        m_pMultiSave->SetSavePath(qstrSavePath);
    }
    ui.lineEdit_FileSavePath->setText(m_pMultiSave->GetSavePath());

    i32Pos = 0;
    ui.lineEdit_MaxFrame->setValidator(pReg_1_65535);
    QString qstrMaxFrame = QString::number(m_pMultiSave->GetMaxFrameVal());
    if (pReg_1_65535->validate(qstrMaxFrame, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetMaxFrameVal(DEFAULT_MAX_FRAME);
    }
    ui.lineEdit_MaxFrame->setText(QString::number(m_pMultiSave->GetMaxFrameVal()));

    i32Pos = 0;
    ui.lineEdit_Time->setValidator(pReg_1_65535);
    QString qstrTime = QString::number(m_pMultiSave->GetTimeVal());
    if (pReg_1_65535->validate(qstrTime, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetTimeVal(DEFAULT_TIME);
    }
    ui.lineEdit_Time->setText(QString::number(m_pMultiSave->GetTimeVal()));

    ui.comboBox_Time->addItem(QObject::tr(TIME_UNIT_SECOND));
    ui.comboBox_Time->addItem(QObject::tr(TIME_UNIT_MINUTE));
    ui.comboBox_Time->addItem(QObject::tr(TIME_UNIT_HOUR));
    ui.comboBox_ImgType->addItem(IMG_TYPE_BMP);
    ui.comboBox_ImgType->addItem(IMG_TYPE_JPEG);
    ui.comboBox_ImgType->addItem(IMG_TYPE_PNG);
    ui.comboBox_ImgType->addItem(IMG_TYPE_TIFF);
    ui.comboBox_ImgType->addItem(IMG_TYPE_RAW);
    ui.comboBox_CfaMethod->addItem(IMG_CFA_QUICK);
    ui.comboBox_CfaMethod->addItem(IMG_CFA_BALANCE);
    ui.comboBox_CfaMethod->addItem(IMG_CFA_OPTIMAL);

    i32Pos = 0;
    ui.lineEdit_ImgQuality->setValidator(pReg_1_100);
    QString qstrImgQuality = QString::number(m_pMultiSave->GetImageQuality());
    if (pReg_1_100->validate(qstrImgQuality, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetImageQuality(DEFAULT_IMAGE_QUALITY);
    }
    ui.lineEdit_ImgQuality->setText(QString::number(m_pMultiSave->GetImageQuality()));

    i32Pos = 0;
    ui.lineEdit_SaveImgFrame->setValidator(pReg_1_65535);
    QString qstrSaveImgFrame = QString::number(m_pMultiSave->GetSaveImageFrameVal());
    if (pReg_1_65535->validate(qstrSaveImgFrame, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetSaveImageFrameVal(DEFAULT_IMAGE_FRAME);
    }
    ui.lineEdit_SaveImgFrame->setText(QString::number(m_pMultiSave->GetSaveImageFrameVal()));

    i32Pos = 0;
    ui.lineEdit_SaveImgTime->setValidator(pReg_1_65535);
    QString qstrSaveImgTime = QString::number(m_pMultiSave->GetSaveImageTimeVal());
    if (pReg_1_65535->validate(qstrSaveImgTime, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetSaveImageTimeVal(DEFAULT_IMAGE_TIME);
    }
    ui.lineEdit_SaveImgTime->setText(QString::number(m_pMultiSave->GetSaveImageTimeVal()));

    ui.comboBox_VideoType->addItem(VIDEO_TYPE_AVI);
    ui.comboBox_VideoType->addItem(VIDEO_TYPE_MP4);
    ui.comboBox_VideoType->addItem(VIDEO_TYPE_ORI_AVI);

    i32Pos = 0;
    ui.lineEdit_VideoBitRate->setValidator(pReg_1_50000);
    QString qstrVideoBitRate = QString::number(m_pMultiSave->GetVideoBitRateVal());
    if ((pReg_1_50000->validate(qstrVideoBitRate, i32Pos) == QValidator::Invalid)
        || (m_pMultiSave->GetVideoBitRateVal() < DEFAULT_VIDEO_BIT_RATE))
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetVideoBitRateVal(DEFAULT_VIDEO_BIT_RATE);
    }
    ui.lineEdit_VideoBitRate->setText(QString::number(m_pMultiSave->GetVideoBitRateVal()));

    ui.comboBox_VideoFrameRateType->addItem(QObject::tr(VIDEO_FRAMERATE_TYPE_ORI));
    ui.comboBox_VideoFrameRateType->addItem(QObject::tr(VIDEO_FRAMERATE_TYPE_CUSTOM));

    i32Pos = 0;
    ui.lineEdit_VideoFrameRate->setValidator(pReg_1_10000);
    QString qstrVideoFrameRate = QString::number(m_pMultiSave->GetVideoFrameRate());
    if (pReg_1_10000->validate(qstrVideoFrameRate, i32Pos) == QValidator::Invalid)
    {
        // 配置文件设置值不再范围内时，自动修复
        m_pMultiSave->SetVideoFrameRate(DEFAULT_VIDEO_FRAME_RATE);
    }
    ui.lineEdit_VideoFrameRate->setText(QString::number(m_pMultiSave->GetVideoFrameRate()));

    ui.lineEdit_BufNum->setEnabled(false);
    ui.checkBox_Unlimit->setChecked(m_pMultiSave->GetUnlimit());
    ui.checkBox_Unlimit->setEnabled(false);
    ui.lineEdit_MaxFrame->setEnabled(false);
    ui.checkBox_MaxFrame->setChecked(m_pMultiSave->GetMaxFrame());
    ui.checkBox_MaxFrame->setEnabled(false);
    ui.checkBox_Time->setChecked(m_pMultiSave->GetTime());
    ui.checkBox_Time->setEnabled(false);

    __SetComboboxDefaultValue();

    ui.comboBox_Time->setEnabled(false);
    ui.comboBox_ImgType->setEnabled(false);
    ui.comboBox_CfaMethod->setEnabled(false);
    ui.checkBox_SaveImg->setChecked(m_pMultiSave->GetSaveImg());
    ui.checkBox_SaveImg->setEnabled(false);
    ui.lineEdit_ImgQuality->setEnabled(false);
    ui.checkBox_SaveImgFrame->setChecked(m_pMultiSave->GetSaveImageFrame());
    ui.checkBox_SaveImgFrame->setEnabled(false);
    ui.checkBox_SaveImgTime->setChecked(m_pMultiSave->GetSaveImageTime());
    ui.checkBox_SaveImgTime->setEnabled(false);
    ui.checkBox_SaveVideo->setChecked(m_pMultiSave->GetSaveVideo());
    ui.checkBox_SaveVideo->setEnabled(false);
    ui.comboBox_VideoType->setEnabled(false);
    ui.comboBox_VideoFrameRateType->setEnabled(false);

    ui.lineEdit_VideoBitRate->setEnabled(false);
    ui.lineEdit_VideoFrameRate->setEnabled(false);
    ui.lineEdit_FileSavePath->setEnabled(false);
    ui.pushButton_FileSavePath->setEnabled(false);
    ui.lineEdit_SaveImgFrame->setEnabled(false);
    ui.lineEdit_SaveImgTime->setEnabled(false);
    ui.lineEdit_Time->setEnabled(false);
    ui.pushButton_Start->setEnabled(false);
    ui.pushButton_Stop->setEnabled(false);

    __SetLimitGroup();
    __SetImgVideoGroup();
    __SetImgGroup();

    m_pRefreshTimer = new QTimer(this);
}

//----------------------------------------------------------------------------------
/**
\brief   设置combobox默认值

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__SetComboboxDefaultValue() const
{
    // TimeUnit
    QString qstrTimeUnit = m_pMultiSave->GetTimeUnit();
    if ((qstrTimeUnit != QString(TIME_UNIT_SECOND))
        && (qstrTimeUnit != QString(TIME_UNIT_MINUTE))
        && (qstrTimeUnit != QString(TIME_UNIT_HOUR)))
    {
        // 配置文件设置值不再范围内时，自动修复
        qstrTimeUnit = QString(DEFAULT_TIME_UNIT);
        m_pMultiSave->SetTimeUnit(qstrTimeUnit);
    }
    ui.comboBox_Time->setCurrentText(m_pMultiSave->GetTimeUnit());

    // ImageType
    QString qstrImageType = m_pMultiSave->GetImageType();
    if ((qstrImageType != QString(IMG_TYPE_BMP))
        && (qstrImageType != QString(IMG_TYPE_JPEG))
        && (qstrImageType != QString(IMG_TYPE_PNG))
        && (qstrImageType != QString(IMG_TYPE_TIFF))
        && (qstrImageType != QString(IMG_TYPE_RAW)))
    {
        // 配置文件设置值不再范围内时，自动修复
        qstrImageType = QString(DEFAULT_IMAGE_TYPE);
        m_pMultiSave->SetImageType(qstrImageType);
    }
    ui.comboBox_ImgType->setCurrentText(m_pMultiSave->GetImageType());

    // CfaMethod
    QString qstrCfaMethod = m_pMultiSave->GetCfaMethod();
    if ((qstrCfaMethod != QString(IMG_CFA_BALANCE))
        && (qstrCfaMethod != QString(IMG_CFA_QUICK))
        && (qstrCfaMethod != QString(IMG_CFA_OPTIMAL)))
    {
        // 配置文件设置值不再范围内时，自动修复
        qstrCfaMethod = QString(DEFAULT_IMAGE_CFA);
        m_pMultiSave->SetCfaMethod(qstrCfaMethod);
    }
    ui.comboBox_CfaMethod->setCurrentText(m_pMultiSave->GetCfaMethod());

    // VideoType
    QString qstrVideoType = m_pMultiSave->GetVideoType();
    if ((qstrVideoType != QString(VIDEO_TYPE_AVI))
        && (qstrVideoType != QString(VIDEO_TYPE_MP4))
        && (qstrVideoType != QString(VIDEO_TYPE_ORI_AVI)))
    {
        // 配置文件设置值不再范围内时，自动修复
        qstrVideoType = DEFAULT_VIDEO_TYPE;
        m_pMultiSave->SetVideoType(qstrVideoType);
    }
    ui.comboBox_VideoType->setCurrentText(m_pMultiSave->GetVideoType());

    // VideoFrameRateType
    QString qstrVideoFrameRateType = m_pMultiSave->GetVideoFrameRateType();
    if ((qstrVideoFrameRateType != QString(VIDEO_FRAMERATE_TYPE_ORI))
        && (qstrVideoFrameRateType != QString(VIDEO_FRAMERATE_TYPE_CUSTOM)))
    {
        // 配置文件设置值不再范围内时，自动修复
        qstrVideoFrameRateType = QString(DEFAULT_VIDEO_FRAME_RATE_TYPE);
        m_pMultiSave->SetVideoFrameRateType(qstrVideoFrameRateType);
    }
    ui.comboBox_VideoFrameRateType->setCurrentText(m_pMultiSave->GetVideoFrameRateType());
}

//----------------------------------------------------------------------------------
/**
\brief   设置保存模式按钮组

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__SetLimitGroup()
{
    // 保存模式按钮互斥
    m_pGroupLimit = new QButtonGroup(this);
    m_pGroupLimit->addButton(ui.checkBox_Unlimit, DEFAULT_UNLIMIT_IDX);
    m_pGroupLimit->addButton(ui.checkBox_MaxFrame, DEFAULT_MAXFRAME_IDX);
    m_pGroupLimit->addButton(ui.checkBox_Time, DEFAULT_MAXTIME_IDX);

    // 一个按钮组有且只有一个按钮被选中
    int32_t i32CheckdID = m_pGroupLimit->checkedId();
    switch (i32CheckdID)
    {
    case DEFAULT_UNLIMIT_IDX:
    {
        m_pMultiSave->SetUnlimit(true);
        m_pMultiSave->SetMaxFrame(false);
        m_pMultiSave->SetTime(false);
        break;
    }

    case DEFAULT_MAXFRAME_IDX:
    {
        m_pMultiSave->SetUnlimit(false);
        m_pMultiSave->SetMaxFrame(true);
        m_pMultiSave->SetTime(false);
        break;
    }

    case DEFAULT_MAXTIME_IDX:
    {
        m_pMultiSave->SetUnlimit(false);
        m_pMultiSave->SetMaxFrame(false);
        m_pMultiSave->SetTime(true);
        break;
    }

    default:
    {
        m_pMultiSave->SetUnlimit(true);
        m_pMultiSave->SetMaxFrame(false);
        m_pMultiSave->SetTime(false);
        ui.checkBox_Unlimit->setChecked(true);
        break;
    }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   设置存图存视频按钮组

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__SetImgVideoGroup()
{
    // 存图存视频按钮互斥
    m_pGroupImgVideo = new QButtonGroup(this);
    m_pGroupImgVideo->addButton(ui.checkBox_SaveImg, DEFAULT_SAVEIMG_IDX);
    m_pGroupImgVideo->addButton(ui.checkBox_SaveVideo, DEFAULT_SAVEVIDEO_IDX);

    // 一个按钮组有且只有一个按钮被选中
    int32_t i32CheckdID = m_pGroupImgVideo->checkedId();
    switch (i32CheckdID)
    {
    case DEFAULT_SAVEIMG_IDX:
    {
        m_pMultiSave->SetSaveImg(true);
        m_pMultiSave->SetSaveVideo(false);
        break;
    }

    case DEFAULT_SAVEVIDEO_IDX:
    {
        m_pMultiSave->SetSaveImg(false);
        m_pMultiSave->SetSaveVideo(true);
        break;
    }

    default:
    {
        m_pMultiSave->SetSaveImg(false);
        m_pMultiSave->SetSaveVideo(true);
        ui.checkBox_SaveVideo->setChecked(true);
        break;
    }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   设置存图按钮组

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__SetImgGroup()
{
    // 存图模式按钮互斥
    m_pGroupImg = new QButtonGroup(this);
    m_pGroupImg->addButton(ui.checkBox_SaveImgFrame, DEFAULT_SAVEIMGFRAME_IDX);
    m_pGroupImg->addButton(ui.checkBox_SaveImgTime, DEFAULT_SAVEIMGTIME_IDX);

    // 一个按钮组有且只有一个按钮被选中
    int32_t i32CheckdID = m_pGroupImg->checkedId();
    switch (i32CheckdID)
    {
    case DEFAULT_SAVEIMGFRAME_IDX:
    {
        m_pMultiSave->SetSaveImageFrame(true);
        m_pMultiSave->SetSaveImageTime(false);
        break;
    }

    case DEFAULT_SAVEIMGTIME_IDX:
    {
        m_pMultiSave->SetSaveImageFrame(false);
        m_pMultiSave->SetSaveImageTime(true);
        break;
    }

    default:
    {
        m_pMultiSave->SetSaveImageFrame(true);
        m_pMultiSave->SetSaveImageTime(false);
        ui.checkBox_SaveImgFrame->setChecked(true);
        break;
    }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   加载控件初始参数

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__LoadInitParam()
{
    QString qstrCfgPath = "";

    // 配置文件地址
#ifdef WIN32
    qstrCfgPath = "Galaxy/cfg/viewer/GxVideoImageSave.json";
#else

#endif

    qstrCfgPath = __GetConfigFilePath(qstrCfgPath);
    m_pMultiSave->InitParam(qstrCfgPath);
}

//----------------------------------------------------------------------------------
/**
\brief   保存控件参数

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__RecordParam()
{
    QString qstrCfgPath = "";

    // 配置文件地址
#ifdef WIN32
    qstrCfgPath = "Galaxy/cfg/viewer/GxVideoImageSave.json";
#else

#endif

    qstrCfgPath = __GetConfigFilePath(qstrCfgPath);
    m_pMultiSave->RecordParam(qstrCfgPath);
}

//----------------------------------------------------------------------------------
/**
\brief   获取默认保存路径

\return  QString    默认保存路径
*/
//----------------------------------------------------------------------------------
QString GxVideoImageSave::__GetDefaultSavePath()
{
    QString qstrSavePath = "";

    // 配置文件地址
#ifdef WIN32
    qstrSavePath = "Galaxy/userdata/ImagesAndVideos";
#else

#endif

    qstrSavePath = __GetConfigFilePath(qstrSavePath);
    return qstrSavePath;
}

//----------------------------------------------------------------------------------
/**
\brief   加载语言模块

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__LoadLanguage()
{
    // 获取当前安装包中英文
    int32_t i32LanguageVal = __GetLanguageValue();
    QString qstrTranslateFile = QString(":/GxVideoImageSave/GxVideoImageSave_zh.qm");
    bool bLoadTransApp = false;
    QTextCodec *pCodec = NULL;

    switch (i32LanguageVal)
    {
    case 1:
        // 设置中文语言
        pCodec = QTextCodec::codecForName("GB2312");
        QTextCodec::setCodecForLocale(pCodec);

        bLoadTransApp = m_objTranslator.load(qstrTranslateFile);
        if (bLoadTransApp)
        {
            QCoreApplication::installTranslator(&m_objTranslator);
        }
        break;

    default:
        // 默认使用英文语言
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   读取配置文件获取安装包语言

\return  int32_t        语言值
*/
//----------------------------------------------------------------------------------
int32_t GxVideoImageSave::__GetLanguageValue()
{
    QString qstrIniPath = "";

    // 配置文件地址
#ifdef WIN32
    qstrIniPath = "Galaxy/cfg/viewer/GalaxyView.ini";
#else

#endif

    qstrIniPath = __GetConfigFilePath(qstrIniPath);
    if (!QFile::exists(qstrIniPath))
    {
        return 0;
    }

    // 返回安装包语言
    QSettings objSettings(qstrIniPath, QSettings::IniFormat);
    int32_t i32LanguageVal = objSettings.value("Language", 0).toInt();
    return i32LanguageVal;
}

//----------------------------------------------------------------------------------
/**
\brief   获取配置文件路径
\param   [in]    QString    配置文件路径
\return  QString            配置文件绝对路径
*/
//----------------------------------------------------------------------------------
QString GxVideoImageSave::__GetConfigFilePath(const QString& qstrFile) const
{
    QString qstrCfgPath = qstrFile;

#ifdef WIN32
    if (!__IsCurrentOSLaterWin7())
    {
        qstrCfgPath = QString("Documents/") + qstrCfgPath;
    }

    QProcessEnvironment objEnv = QProcessEnvironment::systemEnvironment();
    QString qstrAllUsersProfile = objEnv.value("ALLUSERSPROFILE") + QString("/");
    qstrCfgPath = qstrAllUsersProfile + qstrCfgPath;
#else

#endif

    return qstrCfgPath;
}

#ifdef WIN32
//----------------------------------------------------------------------------------
/**
\brief   判断系统是否为win7及以上

\return  bool    true当前系统为win7及以上/false当前系统为win7以下
*/
//----------------------------------------------------------------------------------
bool GxVideoImageSave::__IsCurrentOSLaterWin7() const
{
    OSVERSIONINFO stosvi;
    ZeroMemory(&stosvi, sizeof(OSVERSIONINFO));
    stosvi.dwOSVersionInfoSize = sizeof(OSVERSIONINFO);

    GetVersionEx(&stosvi);

    bool bIsWin7 = (6 == stosvi.dwMajorVersion) && (stosvi.dwMinorVersion >= 1);
    bool bIslaterWin7 = stosvi.dwMajorVersion > 6;

    return bIsWin7 || bIslaterWin7;
}
#endif

//----------------------------------------------------------------------------------
/**
\brief   申请录像存图buffer

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__AllocMemory()
{
    if (m_bSnap && m_bStartSave)
    {
        // 申请buffer
        uint64_t ui64PayloadSize = 0;
        ui64PayloadSize = m_objStream->GetPayloadSize();
        int32_t i32BufNum = m_pMultiSave->GetBufNum();
        m_pQueueManager->InitQueue(i32BufNum, ui64PayloadSize);
        m_bBufAlloc = true;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   界面控件更新

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__UpdateUI() const
{
    ui.pushButton_EnumDev->setEnabled(!m_bDevOpen);
    ui.comboBox_DevList->setEnabled(!m_bDevOpen);
    ui.pushButton_OpenDev->setEnabled(!m_bDevOpen
        && (m_vecCurDevInfoList.size() != 0));
    ui.pushButton_CloseDev->setEnabled(m_bDevOpen 
        && !m_bSnap);
    ui.pushButton_StartSnap->setEnabled(m_bDevOpen
        && !m_bSnap);
    ui.pushButton_StopSnap->setEnabled(m_bDevOpen
        && m_bSnap);
    ui.pushButton_OpenProp->setEnabled(m_bDevOpen);
    ui.lineEdit_BufNum->setEnabled(m_bDevOpen 
        && !m_bSnap 
        && !m_bStartSave);
    ui.lineEdit_FileSavePath->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.pushButton_FileSavePath->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.checkBox_Unlimit->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.checkBox_MaxFrame->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.lineEdit_MaxFrame->setEnabled(m_bDevOpen
        && m_pMultiSave->GetMaxFrame() 
        && !m_bStartSave);
    ui.checkBox_Time->setEnabled(m_bDevOpen 
        && !m_bStartSave);
    ui.lineEdit_Time->setEnabled(m_bDevOpen
        && m_pMultiSave->GetTime() 
        && !m_bStartSave);
    ui.comboBox_Time->setEnabled(m_bDevOpen
        && m_pMultiSave->GetTime() 
        && !m_bStartSave);
    ui.checkBox_SaveImg->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.comboBox_ImgType->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveImg() 
        && !m_bStartSave);
    ui.comboBox_CfaMethod->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveImg() 
        && !m_bStartSave);
    ui.lineEdit_ImgQuality->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveImg() 
        && !m_bStartSave);
    ui.checkBox_SaveImgFrame->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveImg() 
        && !m_bStartSave);
    ui.lineEdit_SaveImgFrame->setEnabled(
        m_bDevOpen && m_pMultiSave->GetSaveImg() 
        && m_pMultiSave->GetSaveImageFrame() 
        && !m_bStartSave);
    ui.checkBox_SaveImgTime->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveImg() 
        && !m_bStartSave);
    ui.lineEdit_SaveImgTime->setEnabled(
        m_bDevOpen && m_pMultiSave->GetSaveImg() 
        && m_pMultiSave->GetSaveImageTime() 
        && !m_bStartSave);
    ui.checkBox_SaveVideo->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.comboBox_VideoType->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveVideo() 
        && !m_bStartSave);
    ui.lineEdit_VideoBitRate->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveVideo() 
        && !m_bStartSave);
    ui.comboBox_VideoFrameRateType->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveVideo() 
        && !m_bStartSave);
    ui.lineEdit_VideoFrameRate->setEnabled(m_bDevOpen
        && m_pMultiSave->GetSaveVideo() 
        && (m_pMultiSave->GetVideoFrameRateType() == QString(VIDEO_FRAMERATE_TYPE_CUSTOM)) 
        && !m_bStartSave);
    ui.pushButton_Start->setEnabled(m_bDevOpen
        && !m_bStartSave);
    ui.pushButton_Stop->setEnabled(m_bDevOpen
        && m_bStartSave);
}

//----------------------------------------------------------------------------------
/**
\brief   连接信号槽

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__Connect()
{
    connect(this, SIGNAL(SigStopSave(bool)), this, SLOT(SlotStopSave(bool)));
    connect(m_pRefreshTimer, &QTimer::timeout, this, &GxVideoImageSave::TimeOut_RefreshStatistics);
    connect(this, SIGNAL(SigShowError(QString)), this, SLOT(ProcessShowError(QString)));
    connect(this, SIGNAL(SigShowInfo(QString)), this, SLOT(ProcessShowInfo(QString)));
    connect(this, SIGNAL(SigSetImg(QPixmap)), this, SLOT(SetImg(QPixmap)));
    connect(ui.pushButton_EnumDev, SIGNAL(clicked()), this, SLOT(ClickBtn_EnumDev()));
    connect(ui.pushButton_OpenDev, SIGNAL(clicked()), this, SLOT(ClickBtn_OpenDev()));
    connect(ui.pushButton_CloseDev, SIGNAL(clicked()), this, SLOT(ClickBtn_CloseDev()));
    connect(ui.pushButton_StartSnap, SIGNAL(clicked()), this, SLOT(ClickBtn_StartSnap()));
    connect(ui.pushButton_StopSnap, SIGNAL(clicked()), this, SLOT(ClickBtn_StopSnap()));
    connect(ui.pushButton_OpenProp, SIGNAL(clicked()), this, SLOT(ClickBtn_OpenProp()));
    connect(ui.lineEdit_BufNum, SIGNAL(editingFinished()), this, SLOT(editingFinished_BufNum()));
    connect(ui.checkBox_ShowImg, SIGNAL(clicked(bool)), this, SLOT(ClickCheckBox_ShowImg(bool)));
    connect(m_pGroupLimit, SIGNAL(buttonClicked(int)), this, SLOT(ClickBtn_Limit(int)));
    connect(m_pGroupImgVideo, SIGNAL(buttonClicked(int)), this, SLOT(ClickBtn_ImgVideo(int)));
    connect(m_pGroupImg, SIGNAL(buttonClicked(int)), this, SLOT(ClickBtn_Img(int)));
    connect(ui.pushButton_Start, SIGNAL(clicked()), this, SLOT(ClickBtn_StartRecord()));
    connect(ui.pushButton_Stop, SIGNAL(clicked()), this, SLOT(ClickBtn_StopRecord()));
    connect(ui.lineEdit_FileSavePath, SIGNAL(editingFinished()), this, SLOT(editingFinished_SavePath()));
    connect(ui.lineEdit_MaxFrame, SIGNAL(editingFinished()), this, SLOT(editingFinished_MaxFrame()));
    connect(ui.lineEdit_Time, SIGNAL(editingFinished()), this, SLOT(editingFinished_Time()));
    connect(ui.comboBox_Time, SIGNAL(currentIndexChanged(int)), this, SLOT(IndexChange_TimeUnit(int)));
    connect(ui.comboBox_ImgType, SIGNAL(currentIndexChanged(int)), this, SLOT(IndexChange_ImgType(int)));
    connect(ui.comboBox_CfaMethod, SIGNAL(currentIndexChanged(int)), this, SLOT(IndexChange_CfaMethod(int)));
    connect(ui.lineEdit_ImgQuality, SIGNAL(editingFinished()), this, SLOT(editingFinished_ImgQuality()));
    connect(ui.lineEdit_SaveImgFrame, SIGNAL(editingFinished()), this, SLOT(editingFinished_SaveImgFrame()));
    connect(ui.lineEdit_SaveImgTime, SIGNAL(editingFinished()), this, SLOT(editingFinished_SaveImgTime()));
    connect(ui.comboBox_VideoType, SIGNAL(currentIndexChanged(int)), this, SLOT(IndexChange_VideoType(int)));
    connect(ui.lineEdit_VideoBitRate, SIGNAL(editingFinished()), this, SLOT(editingFinished_VideoBitRate()));
    connect(ui.comboBox_VideoFrameRateType, SIGNAL(currentIndexChanged(int))
        , this, SLOT(IndexChange_VideoFrameRateType(int)));
    connect(ui.lineEdit_VideoFrameRate, SIGNAL(editingFinished()), this, SLOT(editingFinished_VideoFrameRate()));
    connect(ui.pushButton_FileSavePath, SIGNAL(clicked()), this, SLOT(ClickBtn_SavePath()));
}

//----------------------------------------------------------------------------------
/**
\brief   主线程显示图像

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::SetImg(QPixmap objPixmap)
{
    ui.label_ImageShow->setAlignment(Qt::AlignCenter);
    ui.label_ImageShow->setPixmap(objPixmap);
}

//----------------------------------------------------------------------------------
/**
\brief   点击设备枚举按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_EnumDev()
{
    try
    {
        // 枚举设备
        m_vecCurDevInfoList.clear();
        IGXFactory::GetInstance().UpdateDeviceList(ONE_SECOND, m_vecCurDevInfoList);

        // 枚举信息添加到控件
        ui.comboBox_DevList->clear();
        size_t nDevInfoListSize = m_vecCurDevInfoList.size();
        for (size_t i = 0; i < nDevInfoListSize; ++i)
        {
            ui.comboBox_DevList->addItem(QString(m_vecCurDevInfoList[i].GetDisplayName()));
        }
        ui.comboBox_DevList->setCurrentIndex(0);

        __UpdateUI();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击打开设备按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_OpenDev()
{
    try
    {
        if (m_objCam.IsNull())
        {
            // 通过sn打开相机
            int32_t i32Idx = 0;
            i32Idx = ui.comboBox_DevList->currentIndex();
            m_objCam = IGXFactory::GetInstance().OpenDeviceBySN(m_vecCurDevInfoList[i32Idx].GetSN()
                , GX_ACCESS_EXCLUSIVE);            
            m_objRemoteDevFeature = m_objCam->GetRemoteFeatureControl();
            m_objLocalDevFeature = m_objCam->GetFeatureControl();

            // 打开流通道
            m_objStream = m_objCam->OpenStream(0);
            m_objStreamFeature = m_objStream->GetFeatureControl();
            
            // 注册掉线回调函数
            m_pDevOfflineCB = std::make_shared<CDeviceOfflineEventHandler>();
            connect(m_pDevOfflineCB.get(), SIGNAL(SigDeviceOffline(QString)), this, SLOT(ProcessSaveError(QString)));
            m_pOfflineHandler = m_objCam->RegisterDeviceOfflineCallback(m_pDevOfflineCB.get(), NULL);

            // 打开相机属性树
            m_objCam->GXCreateWnd(GX_WND_PROP, NULL, &m_hDevPropWnd);

            m_bDevOpen = true;

            __UpdateUI();
        }
    }
    catch (const CGalaxyException& e)
    {
        ClickBtn_CloseDev();

        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        ClickBtn_CloseDev();

        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击关闭设备按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_CloseDev()
{
    try
    {
        if (!m_objCam.IsNull())
        {
            if (!m_objStream.IsNull())
            {
                m_objStream->Close();
                m_objStream = CGXStreamPointer();
            }

            if (m_hDevPropWnd != NULL)
            {
                m_objCam->GXDestroyWnd(m_hDevPropWnd);
                m_hDevPropWnd = NULL;
            }

            if (m_pOfflineHandler != NULL)
            {
                m_objCam->UnregisterDeviceOfflineCallback(m_pOfflineHandler);
                m_pOfflineHandler = NULL;
            }

            m_objCam->Close();
            m_objCam = CGXDevicePointer();
            m_objRemoteDevFeature = CGXFeatureControlPointer();
            m_objLocalDevFeature = CGXFeatureControlPointer();

            m_bDevOpen = false;
            __UpdateUI();
        }
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//---------------------------------------------------------------------------------
/**
\brief   开始采集按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_StartSnap()
{
    try
    {
        m_bSnap = true;

        // 申请buffer
        __AllocMemory();

        // 开始采集
        m_objStream->StartGrab();
        m_objRemoteDevFeature->GetCommandFeature("AcquisitionStart")->Execute();

        if (m_bSnap && m_bStartSave)
        {
            m_pMultiSave->StartTimer();
        }

        // 启动采集线程
        m_pProducerThread = std::make_shared<std::thread>(&GxVideoImageSave::DoOnImageCaptured, this);

        // 启动显示线程
        m_bImgProc = true;
        m_pImgThread = std::make_shared<std::thread>(&GxVideoImageSave::DoOnImgProcessThread, this);

        __UpdateUI();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));

        // 开采失败释放资源
        ClickBtn_StopSnap();
    }
    catch (const std::bad_alloc& e)
    {
        QString qstrMsg = QObject::tr("Allocate picture/video buffer failed!");
        emit SigShowError(qstrMsg);

        // 开采失败释放资源
        ClickBtn_StopSnap();
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));

        // 开采失败释放资源
        ClickBtn_StopSnap();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   停止采集按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_StopSnap()
{
    try
    {
        m_bSnap = false;

        // 停止显示
        m_bImgProc = false;
        if (m_pImgThread != nullptr)
        {
            m_pImgThread->join();
            m_pImgThread = nullptr;
        }

        // 停止录像
        if (m_bStartSave)
        {
            ClickBtn_StopRecord();
        }

        // 停止采集回调
        if (m_pProducerThread != nullptr)
        {
            m_pProducerThread->join();
            m_pProducerThread = nullptr;
        }

        __StopRemoteDevice();

        // 本地停采
        if (!m_objStream.IsNull())
        {
            m_objStream->StopGrab();

            // 反初始化队列
            m_pQueueManager->UnInitQueue();

            m_bBufAlloc = false;
        }

        __UpdateUI();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   打开属性栏

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_OpenProp()
{
    try
    {
        if (!m_objCam.IsNull())
        {
            QPoint Pos = this->frameGeometry().topLeft();
            int32_t i32Width = this->frameGeometry().width();
            int32_t i32Height = this->frameGeometry().height();
            QPoint Point = QPoint(Pos.x() + i32Width, Pos.y());
            m_objCam->GXSetShowPosition(m_hDevPropWnd, Point.x(), Point.y(), DEFAULT_PROP_WIDTH, i32Height);
            /*
            * 下面这个接口是控制台（如Dos程序或python无界面程序等）下启动时需要调用的，
            * 如果您的程序是GUI程序如（Qt、MFC、Winforms等）请勿调用。
            */
            //m_objCam->GXSetShowMode(m_hDevPropWnd, BLOCK_SHOW_MODE);

            m_objCam->GXShowWnd(m_hDevPropWnd, true);
        }
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   远端相机停采

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::__StopRemoteDevice()
{
    try
    {
        // 相机停止采集
        m_objRemoteDevFeature->GetCommandFeature("AcquisitionStop")->Execute();
    }
    catch (const CGalaxyException& e)
    {
        // 相机掉线时报告错误，并继续执行资源释放
        emit SigShowError(QString(e.what()));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击窗口关闭按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::closeEvent(QCloseEvent* pEvent)
{
    try
    {
        // 关闭录像存图
        if (m_bStartSave)
        {
            ClickBtn_StopRecord();
        }

        // 关闭采集
        if (m_bSnap)
        {
            ClickBtn_StopSnap();
        }
    
        // 关闭相机
        if (m_bDevOpen)
        {
            ClickBtn_CloseDev();
        }

        // 记录参数
        __RecordParam();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   采集buffer个数回车回调函数

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_BufNum()
{
    if (ui.lineEdit_BufNum->hasFocus())
    {
        if (ui.lineEdit_BufNum->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_BufNum->setText(QString::number(m_pMultiSave->GetBufNum()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetBufNum(ui.lineEdit_BufNum->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_BufNum->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_BufNum->setText(QString::number(m_pMultiSave->GetBufNum()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetBufNum(ui.lineEdit_BufNum->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击显示图像按钮
\param   [in]    bChecked    true勾选/false不勾选
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickCheckBox_ShowImg(bool bChecked)
{
    m_bShowImg = bChecked;

    if (!m_bShowImg)
    {
        ui.label_ImageShow->setVisible(false);
    }
    else
    {
        ui.label_ImageShow->setVisible(true);
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击保存方式按钮组
\param   [in]    i32Idx    1:无限制/2:按最大帧数保存/3:按最大时间保存
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_Limit(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_UNLIMIT_IDX:
    {
        m_pMultiSave->SetUnlimit(true);
        m_pMultiSave->SetMaxFrame(false);
        m_pMultiSave->SetTime(false);
        ui.lineEdit_MaxFrame->setEnabled(false);
        ui.lineEdit_Time->setEnabled(false);
        ui.comboBox_Time->setEnabled(false);
        break;
    }

    case DEFAULT_MAXFRAME_IDX:
    {
        m_pMultiSave->SetUnlimit(false);
        m_pMultiSave->SetMaxFrame(true);
        m_pMultiSave->SetTime(false);
        ui.lineEdit_MaxFrame->setEnabled(true);
        ui.lineEdit_Time->setEnabled(false);
        ui.comboBox_Time->setEnabled(false);
        break;
    }

    case DEFAULT_MAXTIME_IDX:
    {
        m_pMultiSave->SetUnlimit(false);
        m_pMultiSave->SetMaxFrame(false);
        m_pMultiSave->SetTime(true);
        ui.lineEdit_MaxFrame->setEnabled(false);
        ui.lineEdit_Time->setEnabled(true);
        ui.comboBox_Time->setEnabled(true);
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击录像存图按钮组
\param   [in]    i32Idx    1:存图/2:录像
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_ImgVideo(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_SAVEIMG_IDX:
    {
        m_pMultiSave->SetSaveImg(true);
        m_pMultiSave->SetSaveVideo(false);
        ui.comboBox_ImgType->setEnabled(true);
        ui.comboBox_CfaMethod->setEnabled(true);
        ui.lineEdit_ImgQuality->setEnabled(true);
        ui.checkBox_SaveImgFrame->setEnabled(true);
        ui.lineEdit_SaveImgFrame->setEnabled(m_pMultiSave->GetSaveImageFrame());
        ui.checkBox_SaveImgTime->setEnabled(true);
        ui.lineEdit_SaveImgTime->setEnabled(m_pMultiSave->GetSaveImageTime());
        ui.comboBox_VideoType->setEnabled(false);
        ui.lineEdit_VideoBitRate->setEnabled(false);
        ui.comboBox_VideoFrameRateType->setEnabled(false);
        ui.lineEdit_VideoFrameRate->setEnabled(false);
        break;
    }

    case DEFAULT_SAVEVIDEO_IDX:
    {
        m_pMultiSave->SetSaveImg(false);
        m_pMultiSave->SetSaveVideo(true);
        ui.comboBox_ImgType->setEnabled(false);
        ui.comboBox_CfaMethod->setEnabled(false);
        ui.lineEdit_ImgQuality->setEnabled(false);
        ui.checkBox_SaveImgFrame->setEnabled(false);
        ui.lineEdit_SaveImgFrame->setEnabled(false);
        ui.checkBox_SaveImgTime->setEnabled(false);
        ui.lineEdit_SaveImgTime->setEnabled(false);
        ui.comboBox_VideoType->setEnabled(true);
        ui.lineEdit_VideoBitRate->setEnabled(true);
        ui.comboBox_VideoFrameRateType->setEnabled(true);
        ui.lineEdit_VideoFrameRate->setEnabled(
            m_pMultiSave->GetVideoFrameRateType() == QString(VIDEO_FRAMERATE_TYPE_CUSTOM));
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击存图方式按钮
\param   [in]    i32Idx    1:以帧间隔存图/2:以时间间隔存图
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_Img(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_SAVEIMGFRAME_IDX:
    {
        // 以帧间隔存图
        m_pMultiSave->SetSaveImageFrame(true);
        m_pMultiSave->SetSaveImageTime(false);
        ui.lineEdit_SaveImgFrame->setEnabled(true);
        ui.lineEdit_SaveImgTime->setEnabled(false);
        break;
    }

    case DEFAULT_SAVEIMGTIME_IDX:
    {
        // 以时间间隔存图
        m_pMultiSave->SetSaveImageFrame(false);
        m_pMultiSave->SetSaveImageTime(true);
        ui.lineEdit_SaveImgFrame->setEnabled(false);
        ui.lineEdit_SaveImgTime->setEnabled(true);
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击保存路径按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_SavePath()
{
    // 弹出路径选择界面
    QString qstrFolderPath = QFileDialog::getExistingDirectory(this
        , QObject::tr("Choose Folder")
        , m_pMultiSave->GetSavePath()
        , QFileDialog::ShowDirsOnly | QFileDialog::DontResolveSymlinks);
    if (qstrFolderPath.isEmpty())
    {
        ui.lineEdit_FileSavePath->setText(m_pMultiSave->GetSavePath());
        return;
    }

    // 文件名称显示到路径输入框中
    ui.lineEdit_FileSavePath->setText(qstrFolderPath);
    m_pMultiSave->SetSavePath(qstrFolderPath);
}

//----------------------------------------------------------------------------------
/**
\brief   保存路径回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_SavePath()
{
    QString qstrFolderPath = ui.lineEdit_FileSavePath->text();
    QDir objDir(qstrFolderPath);

    // 判断路径是否存在
    if (!objDir.exists(qstrFolderPath))
    {
        // 判断路径创建是否成功
        if (!objDir.mkpath(qstrFolderPath))
        {
            QMessageBox::critical(this, QObject::tr("The path error"), QObject::tr("The path entered is invalid!"));

            ui.lineEdit_FileSavePath->setText(m_pMultiSave->GetSavePath());
            return;
        }
    }

    m_pMultiSave->SetSavePath(qstrFolderPath);
}

//----------------------------------------------------------------------------------
/**
\brief   最大保存帧数回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_MaxFrame()
{
    if (ui.lineEdit_MaxFrame->hasFocus())
    {
        if (ui.lineEdit_MaxFrame->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_MaxFrame->setText(QString::number(m_pMultiSave->GetMaxFrameVal()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetMaxFrameVal(ui.lineEdit_MaxFrame->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_MaxFrame->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_MaxFrame->setText(QString::number(m_pMultiSave->GetMaxFrameVal()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetMaxFrameVal(ui.lineEdit_MaxFrame->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   最大保存时间回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_Time()
{
    if (ui.lineEdit_Time->hasFocus())
    {
        if (ui.lineEdit_Time->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_Time->setText(QString::number(m_pMultiSave->GetTimeVal()));
        }
        else
        {
            // 回车输入有效字符
            int32_t i32TimeVal = 0;
            int32_t i32TimeRealVal = 0;
            i32TimeVal = ui.lineEdit_Time->text().toInt();
            m_pMultiSave->SetTimeVal(i32TimeVal);
            i32TimeRealVal = __CalcRealTime(i32TimeVal);
            m_pMultiSave->SetTimeRealVal(i32TimeRealVal);
        }
    }
    else
    {
        if (ui.lineEdit_Time->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_Time->setText(QString::number(m_pMultiSave->GetTimeVal()));
        }
        else
        {
            // 失焦输入有效字符
            int32_t i32TimeVal = 0;
            int32_t i32TimeRealVal = 0;
            i32TimeVal = ui.lineEdit_Time->text().toInt();
            m_pMultiSave->SetTimeVal(i32TimeVal);
            i32TimeRealVal = __CalcRealTime(i32TimeVal);
            m_pMultiSave->SetTimeRealVal(i32TimeRealVal);
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   计算实际时间
\param   [in]    i32Time    输入时间
\return  int32_t    实际时间，单位：秒
*/
//----------------------------------------------------------------------------------
int32_t GxVideoImageSave::__CalcRealTime(const int32_t& i32Time) const
{
    // 根据单位计算实际时间
    int32_t i32TimeRealVal = 0;
    i32TimeRealVal = m_pMultiSave->GetTimeRealVal();
    QString qstrTimeUnit = m_pMultiSave->GetTimeUnit();
    if (qstrTimeUnit == QString(TIME_UNIT_MINUTE))
    {
        i32TimeRealVal = i32Time * TIME_CONVERT_VALUE;
    }
    else if(qstrTimeUnit == QString(TIME_UNIT_HOUR))
    {
        i32TimeRealVal = i32Time * TIME_CONVERT_VALUE * TIME_CONVERT_VALUE;
    }
    else
    {
        i32TimeRealVal = i32Time;
    }

    return i32TimeRealVal;
}

//----------------------------------------------------------------------------------
/**
\brief   选择时间单位回调
\param   [in]    i32Idx    0:second/1:minute/2:hour
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::IndexChange_TimeUnit(int i32Idx)
{
    int32_t i32TimeRealVal = 0;
    int32_t i32TimeVal = 0;
    i32TimeRealVal = m_pMultiSave->GetTimeRealVal();
    i32TimeVal = m_pMultiSave->GetTimeVal();

    // 设置单位，计算实际时间
    switch (i32Idx)
    {
    case DEFAULT_SECOND_IDX:
    {
        m_pMultiSave->SetTimeUnit(QString(TIME_UNIT_SECOND));
        i32TimeRealVal = i32TimeVal;
        break;
    }

    case DEFAULT_MINUTE_IDX:
    {
        m_pMultiSave->SetTimeUnit(QString(TIME_UNIT_MINUTE));
        i32TimeRealVal = i32TimeVal * TIME_CONVERT_VALUE;
        break;
    }

    case DEFAULT_HOUR_IDX:
    {
        m_pMultiSave->SetTimeUnit(QString(TIME_UNIT_HOUR));
        i32TimeRealVal = i32TimeVal * TIME_CONVERT_VALUE * TIME_CONVERT_VALUE;
        break;
    }

    default:
        break;
    }

    m_pMultiSave->SetTimeRealVal(i32TimeRealVal);
}

//----------------------------------------------------------------------------------
/**
\brief   选择图像类型回调
\param   [in]    i32Idx    0:bmp/1:jpeg/2:png/3:tiff/4:raw
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::IndexChange_ImgType(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_BMP_IDX:
    {
        m_pMultiSave->SetImageType(QString(IMG_TYPE_BMP));
        break;
    }

    case DEFAULT_JPEG_IDX:
    {
        m_pMultiSave->SetImageType(QString(IMG_TYPE_JPEG));
        break;
    }

    case DEFAULT_PNG_IDX:
    {
        m_pMultiSave->SetImageType(QString(IMG_TYPE_PNG));
        break;
    }

    case DEFAULT_TIFF_IDX:
    {
        m_pMultiSave->SetImageType(QString(IMG_TYPE_TIFF));
        break;
    }

    case DEFAULT_RAW_IDX:
    {
        m_pMultiSave->SetImageType(QString(IMG_TYPE_RAW));
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   选择插值方式回调
\param   [in]    i32Idx    0:quick/1:balance/2:optimal
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::IndexChange_CfaMethod(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_QUICK_IDX:
    {
        m_pMultiSave->SetCfaMethod(QString(IMG_CFA_QUICK));
        break;
    }

    case DEFAULT_BALANCE_IDX:
    {
        m_pMultiSave->SetCfaMethod(QString(IMG_CFA_BALANCE));
        break;
    }

    case DEFAULT_OPTIMAL_IDX:
    {
        m_pMultiSave->SetCfaMethod(QString(IMG_CFA_OPTIMAL));
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   图像质量回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_ImgQuality()
{
    if (ui.lineEdit_ImgQuality->hasFocus())
    {
        if (ui.lineEdit_ImgQuality->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_ImgQuality->setText(QString::number(m_pMultiSave->GetImageQuality()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetImageQuality(ui.lineEdit_ImgQuality->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_ImgQuality->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_ImgQuality->setText(QString::number(m_pMultiSave->GetImageQuality()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetImageQuality(ui.lineEdit_ImgQuality->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   保存图像帧数回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_SaveImgFrame()
{
    if (ui.lineEdit_SaveImgFrame->hasFocus())
    {
        if (ui.lineEdit_SaveImgFrame->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_SaveImgFrame->setText(QString::number(m_pMultiSave->GetSaveImageFrameVal()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetSaveImageFrameVal(ui.lineEdit_SaveImgFrame->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_SaveImgFrame->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_SaveImgFrame->setText(QString::number(m_pMultiSave->GetSaveImageFrameVal()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetSaveImageFrameVal(ui.lineEdit_SaveImgFrame->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   保存图像时间回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_SaveImgTime()
{
    if (ui.lineEdit_SaveImgTime->hasFocus())
    {
        if (ui.lineEdit_SaveImgTime->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_SaveImgTime->setText(QString::number(m_pMultiSave->GetSaveImageTimeVal()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetSaveImageTimeVal(ui.lineEdit_SaveImgTime->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_SaveImgTime->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_SaveImgTime->setText(QString::number(m_pMultiSave->GetSaveImageTimeVal()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetSaveImageTimeVal(ui.lineEdit_SaveImgTime->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   选择录像类型回调
\param   [in]    i32Idx    0:avi/1:mp4
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::IndexChange_VideoType(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_AVI_IDX:
    {
        m_pMultiSave->SetVideoType(QString(VIDEO_TYPE_AVI));
        break;
    }

    case DEFAULT_MP4_IDX:
    {
        m_pMultiSave->SetVideoType(QString(VIDEO_TYPE_MP4));
        break;
    }

    case DEFAULT_ORIAVI_IDX:
    {
        m_pMultiSave->SetVideoType(QString(VIDEO_TYPE_ORI_AVI));
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   设置录像比特率回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_VideoBitRate()
{
    if (ui.lineEdit_VideoBitRate->text().isEmpty())
    {
        ui.lineEdit_VideoBitRate->setText(QString::number(m_pMultiSave->GetVideoBitRateVal()));
    }
    else
    {
        int32_t i32Value = 0;
        i32Value = ui.lineEdit_VideoBitRate->text().toInt();
        if (i32Value < MIN_VEDIOBITRATE)
        {
            // 范围外恢复默认
            ui.lineEdit_VideoBitRate->setText(QString::number(m_pMultiSave->GetVideoBitRateVal()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetVideoBitRateVal(ui.lineEdit_VideoBitRate->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   选择录像帧率模式回调
\param   [in]    i32Idx    0:原始模式/1:自定义模式
\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::IndexChange_VideoFrameRateType(int i32Idx)
{
    switch (i32Idx)
    {
    case DEFAULT_FRAMETYPE_ORI_IDX:
    {
        m_pMultiSave->SetVideoFrameRateType(QString(VIDEO_FRAMERATE_TYPE_ORI));
        ui.lineEdit_VideoFrameRate->setEnabled(false);
        break;
    }

    case DEFAULT_FRAMETYPE_CUSTOM_IDX:
    {
        m_pMultiSave->SetVideoFrameRateType(QString(VIDEO_FRAMERATE_TYPE_CUSTOM));
        ui.lineEdit_VideoFrameRate->setEnabled(true);
        break;
    }

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   录像帧率回车回调

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::editingFinished_VideoFrameRate()
{
    if (ui.lineEdit_VideoFrameRate->hasFocus())
    {
        if (ui.lineEdit_VideoFrameRate->text().isEmpty())
        {
            // 回车输入空字符串时恢复默认
            ui.lineEdit_VideoFrameRate->setText(QString::number(m_pMultiSave->GetVideoFrameRate()));
        }
        else
        {
            // 回车输入有效字符
            m_pMultiSave->SetVideoFrameRate(ui.lineEdit_VideoFrameRate->text().toInt());
        }
    }
    else
    {
        if (ui.lineEdit_VideoFrameRate->text().isEmpty())
        {
            // 失焦时为空字符串时回复默认
            ui.lineEdit_VideoFrameRate->setText(QString::number(m_pMultiSave->GetVideoFrameRate()));
        }
        else
        {
            // 失焦输入有效字符
            m_pMultiSave->SetVideoFrameRate(ui.lineEdit_VideoFrameRate->text().toInt());
        }
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击开始录制按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_StartRecord()
{
    try
    {
        m_bStartSave = true;

        // 申请内存
        __AllocMemory();

        // 存图存视频参数初始化
        m_pMultiSave->InitStartParam(m_objLocalDevFeature, m_objRemoteDevFeature, m_objStreamFeature);

        if (m_bSnap && m_bStartSave)
        {
            m_pMultiSave->StartTimer();
        }

        // 启动存图存视频线程
        m_pRecorderThread = std::make_shared<std::thread>(&GxVideoImageSave::DoOnRecorderThread, this);

        // 启动统计计数定时器
        m_pRefreshTimer->start(DEFAULT_REFRESH_TIME_INTERVAL);

        __UpdateUI();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));

        // 开始记录失败，释放资源
        ClickBtn_StopRecord();
    }
    catch (const std::bad_alloc& e)
    {
        QString qstrMsg = QObject::tr("Allocate picture/video buffer failed!");
        emit SigShowError(qstrMsg);

        // 开始记录失败，释放资源
        ClickBtn_StopRecord();
    }
    catch (...)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));

        // 开始记录失败，释放资源
        ClickBtn_StopRecord();
    }
}

//----------------------------------------------------------------------------------
/**
\brief   点击停止录制按钮

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::ClickBtn_StopRecord()
{
    __UpdateUI();

    // 停止保存
    emit SigStopSave(true);
}

//----------------------------------------------------------------------------------
/**
\brief   停止录制

\return  无
*/
//----------------------------------------------------------------------------------
void GxVideoImageSave::SlotStopSave(bool bManualStop)
{
    try
    {
        // 将开始保存标志置为false
        m_bStartSave = false;

        m_bManualStop = bManualStop;

        // 停止计时器
        m_pMultiSave->StopTimer();

        // 停止存图存视频线程
        if (m_pRecorderThread != nullptr)
        {
            while (m_bThreadIsRunning)
            {
                // 使界面不阻塞
                QApplication::processEvents();
            }

            if (m_pRecorderThread == nullptr)
            {
                return;
            }

            m_pRecorderThread->join();
            m_pRecorderThread = nullptr;
        }

        // 释放存图存视频资源
        m_pMultiSave->DestroyParam();

        // 停止统计计数定时器
        m_pRefreshTimer->stop();

        // 手动刷新一次统计信息
        TimeOut_RefreshStatistics();

        QString strMsg = QString(tr("Saving picture/video has stopped!"));
        emit SigShowInfo(strMsg);

        /*
        * 单通道内存下，当分配大Buffer，同时开采与存图时，第二次、三次等后续存图可能比第一次存图数量少，
        * 此时可以调用如下代码，重置一下内存解决。
        */
        m_pQueueManager->ResetMem();

        // 将消费队列转移到生产队列
        uint32_t ui32ConsumerSize = m_pQueueManager->GetElemSize(CONSUMER);
        for (uint32_t i = 0; i < ui32ConsumerSize; ++i)
        {
            std::shared_ptr<HV_FRAME_INFO> pFrame = m_pQueueManager->PopFront(CONSUMER);
            m_pQueueManager->PushBack(PRODUCER, pFrame);
        }

        __UpdateUI();
    }
    catch (const CGalaxyException& e)
    {
        emit SigShowError(QString(e.what()));
    }
    catch (std::exception e)
    {
        emit SigShowError(QString(QObject::tr("Unknown Error")));
    }
}

//----------------------------------------------------------------------------------
/**
\brief   判断是否为彩色图像
\param   [in]    pFrame    输入图像
\return  bool    true彩色图像/false黑白图像
*/
//----------------------------------------------------------------------------------
bool GxVideoImageSave::__IsColor(std::shared_ptr<HV_FRAME_INFO> pFrame) const
{
    uint32_t ui32Pixel = 0;
    ui32Pixel = pFrame->nRawPixelFormat;

    //将图像格式和下述宏定义做按位与（&）运算，可判断像素格式是mono还是RGB
    const uint32_t ui32PixelMono = 0x01000000;               //判断是否为MONO格式的掩码
    const uint32_t ui32PixelRgb = 0x20000000;                //判断是否为RGB格式的掩码 
    const uint32_t ui32PixelMonoRgbCustom = 0x80000000U;     //判断是否为MONO格式的掩码 
    const uint32_t ui32PixelColorMask = 0xFF000000U;         //判断是否为彩色格式的掩码

    //将图像格式与下述宏定义做按位与（&）运算，可得到像素格式的ID
    uint32_t ui32PixelIdMask = 0x0000FFFF;

    bool bIsMono = ((ui32PixelColorMask & ui32Pixel) == ui32PixelMono); // 是否为mono格式

    bool bIsRgb = ((ui32PixelColorMask & ui32Pixel) == ui32PixelRgb);  // 是否为RGB格式           
    bool bIsBayer = __IsBayer(ui32Pixel);   // 是否为Bayer格式

    return !(bIsMono && (!bIsBayer) && (!bIsRgb));  // 用于判断是否为黑白相机
}

//----------------------------------------------------------------------------------
/**
\brief   判断是否为bayer图像
\param   [in]    nPixelFormat    输入像素格式
\return  bool    true bayer图像/false 非bayer图像
*/
//----------------------------------------------------------------------------------
bool GxVideoImageSave::__IsBayer(const int32_t& nPixelFormat) const
{
    bool bIsBayer = false;
    switch (nPixelFormat)
    {
    case GX_PIXEL_FORMAT_BAYER_GR8:
    case GX_PIXEL_FORMAT_BAYER_RG8:
    case GX_PIXEL_FORMAT_BAYER_GB8:
    case GX_PIXEL_FORMAT_BAYER_BG8:
    case GX_PIXEL_FORMAT_BAYER_GR10:
    case GX_PIXEL_FORMAT_BAYER_RG10:
    case GX_PIXEL_FORMAT_BAYER_GB10:
    case GX_PIXEL_FORMAT_BAYER_BG10:
    case GX_PIXEL_FORMAT_BAYER_GR12:
    case GX_PIXEL_FORMAT_BAYER_RG12:
    case GX_PIXEL_FORMAT_BAYER_GB12:
    case GX_PIXEL_FORMAT_BAYER_BG12:
    case GX_PIXEL_FORMAT_BAYER_GR14:
    case GX_PIXEL_FORMAT_BAYER_RG14:
    case GX_PIXEL_FORMAT_BAYER_GB14:
    case GX_PIXEL_FORMAT_BAYER_BG14:
    case GX_PIXEL_FORMAT_BAYER_GR16:
    case GX_PIXEL_FORMAT_BAYER_RG16:
    case GX_PIXEL_FORMAT_BAYER_GB16:
    case GX_PIXEL_FORMAT_BAYER_BG16:
    case GX_PIXEL_FORMAT_BAYER_GR10_P:
    case GX_PIXEL_FORMAT_BAYER_RG10_P:
    case GX_PIXEL_FORMAT_BAYER_GB10_P:
    case GX_PIXEL_FORMAT_BAYER_BG10_P:
    case GX_PIXEL_FORMAT_BAYER_GR12_P:
    case GX_PIXEL_FORMAT_BAYER_RG12_P:
    case GX_PIXEL_FORMAT_BAYER_GB12_P:
    case GX_PIXEL_FORMAT_BAYER_BG12_P:
    case GX_PIXEL_FORMAT_BAYER_GR14_P:
    case GX_PIXEL_FORMAT_BAYER_RG14_P:
    case GX_PIXEL_FORMAT_BAYER_GB14_P:
    case GX_PIXEL_FORMAT_BAYER_BG14_P:
    case GX_PIXEL_FORMAT_BAYER_BG10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GB10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_RG10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_BG12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GB12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_RG12_PACKED:
        bIsBayer = true;
        break;
    default:
        break;
    }

    return bIsBayer;
}

//----------------------------------------------------------------------------------
/**
\brief   获取最佳像素位
\param   [in]    emPixelFormatEntry    输入像素格式
\return  GX_VALID_BIT_LIST    最佳像素位
*/
//----------------------------------------------------------------------------------
GX_VALID_BIT_LIST GxVideoImageSave::__GetBestValudBit(GX_PIXEL_FORMAT_ENTRY emPixelFormatEntry) const
{
    GX_VALID_BIT_LIST emValidBits = GX_BIT_0_7;
    switch (emPixelFormatEntry)
    {
    case GX_PIXEL_FORMAT_R8:
    case GX_PIXEL_FORMAT_G8:
    case GX_PIXEL_FORMAT_B8:
    case GX_PIXEL_FORMAT_MONO8:
    case GX_PIXEL_FORMAT_BAYER_GR8:
    case GX_PIXEL_FORMAT_BAYER_RG8:
    case GX_PIXEL_FORMAT_BAYER_GB8:
    case GX_PIXEL_FORMAT_BAYER_BG8:
    {
        emValidBits = GX_BIT_0_7;
        break;
    }
    case GX_PIXEL_FORMAT_MONO10:
    case GX_PIXEL_FORMAT_MONO10_P:
    case GX_PIXEL_FORMAT_MONO10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR10:
    case GX_PIXEL_FORMAT_BAYER_RG10:
    case GX_PIXEL_FORMAT_BAYER_GB10:
    case GX_PIXEL_FORMAT_BAYER_BG10:
    case GX_PIXEL_FORMAT_BAYER_BG10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GB10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_RG10_PACKED:
    case GX_PIXEL_FORMAT_BAYER_BG10_P:
    case GX_PIXEL_FORMAT_BAYER_GB10_P:
    case GX_PIXEL_FORMAT_BAYER_GR10_P:
    case GX_PIXEL_FORMAT_BAYER_RG10_P:
    {
        emValidBits = GX_BIT_2_9;
        break;
    }
    case GX_PIXEL_FORMAT_MONO12:
    case GX_PIXEL_FORMAT_MONO12_P:
    case GX_PIXEL_FORMAT_MONO12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR12:
    case GX_PIXEL_FORMAT_BAYER_RG12:
    case GX_PIXEL_FORMAT_BAYER_GB12:
    case GX_PIXEL_FORMAT_BAYER_BG12:
    case GX_PIXEL_FORMAT_BAYER_BG12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GB12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_GR12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_RG12_PACKED:
    case GX_PIXEL_FORMAT_BAYER_BG12_P:
    case GX_PIXEL_FORMAT_BAYER_GB12_P:
    case GX_PIXEL_FORMAT_BAYER_GR12_P:
    case GX_PIXEL_FORMAT_BAYER_RG12_P:
    {
        emValidBits = GX_BIT_4_11;
        break;
    }
    case GX_PIXEL_FORMAT_MONO14:
    case GX_PIXEL_FORMAT_MONO14_P:
    case GX_PIXEL_FORMAT_BAYER_GR14:
    case GX_PIXEL_FORMAT_BAYER_RG14:
    case GX_PIXEL_FORMAT_BAYER_GB14:
    case GX_PIXEL_FORMAT_BAYER_BG14:
    case GX_PIXEL_FORMAT_BAYER_GR14_P:
    case GX_PIXEL_FORMAT_BAYER_RG14_P:
    case GX_PIXEL_FORMAT_BAYER_GB14_P:
    case GX_PIXEL_FORMAT_BAYER_BG14_P:
    {
        emValidBits = GX_BIT_6_13;
        break;
    }
    case GX_PIXEL_FORMAT_MONO16:
    case GX_PIXEL_FORMAT_BAYER_GR16:
    case GX_PIXEL_FORMAT_BAYER_RG16:
    case GX_PIXEL_FORMAT_BAYER_GB16:
    case GX_PIXEL_FORMAT_BAYER_BG16:
    {
        emValidBits = GX_BIT_8_15;
        break;
    }
    default:
        //返回默认值GX_BIT_0_7
        break;
    }
    return emValidBits;
}