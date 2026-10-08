#include "CQueueManager.h"

//----------------------------------------------------------------------------------
/**
\brief   构造函数

\return  无
*/
//----------------------------------------------------------------------------------
CQueueManager::CQueueManager()
    : m_ui64PayloadSize(0)
    , m_i32BufNum(0)
    , m_i32TotalBufNum(0)
    , m_bInit(false)
{
}

//----------------------------------------------------------------------------------
/**
\brief   析构函数

\return  无
*/
//----------------------------------------------------------------------------------
CQueueManager::~CQueueManager()
{
}

//----------------------------------------------------------------------------------
/**
\brief   初始化buffer集合
\param   [in]    i32BufNum        采集buffer数
\param   [in]    ui64PayloadSize  buffer大小
\param   [in]    ui64Alignment    对齐要求
\return  无
*/
//----------------------------------------------------------------------------------
void CQueueManager::InitQueue(const int32_t& i32TotalBufNum
    , const uint64_t& ui64PayloadSize)
{
    if (m_bInit)
    {
        return;
    }
    else
    {
        m_bInit = true;
    }

    if (i32TotalBufNum > INIT_BUF_NUM)
    {
        m_i32BufNum = INIT_BUF_NUM;
    }
    else
    {
        m_i32BufNum = i32TotalBufNum;
    }

    m_ui64PayloadSize = ui64PayloadSize;
    m_i32TotalBufNum = i32TotalBufNum;

    // 初始化生产者队列
    for (uint32_t i = 0; i < (uint32_t)m_i32BufNum; ++i)
    {
        std::shared_ptr<HV_FRAME_INFO> pFrame = std::make_shared<HV_FRAME_INFO>();
        pFrame->pBuffer = std::shared_ptr<uint8_t[]>(new uint8_t[ui64PayloadSize]
            , std::default_delete<uint8_t[]>());

        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        m_listProducer.push_back(pFrame);
    }
}


//----------------------------------------------------------------------------------
/**
\brief   反初始化队列

\return  无
*/
//----------------------------------------------------------------------------------
void CQueueManager::UnInitQueue()
{
    m_bInit = false;

    {
        // 释放录像存图队列
        std::lock_guard<std::mutex> objLock(m_objConsumerLock);
        m_listConsumer.clear();
    }

    {
        // 释放采集队列
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        m_listProducer.clear();
    }

    {
        // 释放显示队列
        std::lock_guard<std::mutex> objLock(m_objShowLock);
        m_listShow.clear();
    }

}

//----------------------------------------------------------------------------------
/**
\brief    重置内存

\return  无
*/
//----------------------------------------------------------------------------------
void CQueueManager::ResetMem()
{
    {
        // 采集队列重置
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        for (auto const& item : m_listProducer)
        {
            memset(item->pBuffer.get(), 0, m_ui64PayloadSize);
        }
    }

    {
        // 录像存图队列重置
        std::lock_guard<std::mutex> objLock(m_objConsumerLock);
        for (auto const& item : m_listConsumer)
        {
            memset(item->pBuffer.get(), 0, m_ui64PayloadSize);
        }
    }


}

//----------------------------------------------------------------------------------
/**
\brief   获取队列大小
\param   [in]    emType    队列类型
\return  uint32_t    队列大小
*/
//----------------------------------------------------------------------------------
uint32_t CQueueManager::GetElemSize(QUEUE_TYPE emType)
{
    switch (emType)
    {
    case PRODUCER:
    {
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        return (uint32_t)m_listProducer.size();
    }

    case CONSUMER:
    {
        std::lock_guard<std::mutex> objLock(m_objConsumerLock);
        return (uint32_t)m_listConsumer.size();
    }

    default:
        return 0;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   弹出队列头图像
\param   [in]    emType    队列类型
\return  CImageDataPointer    队列头图像
*/
//----------------------------------------------------------------------------------
std::shared_ptr<HV_FRAME_INFO> CQueueManager::PopFront(QUEUE_TYPE emType)
{
    std::shared_ptr<HV_FRAME_INFO> pElem = nullptr;

    switch (emType)
    {
    case PRODUCER:
    {
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        if (!m_listProducer.empty())
        {
            pElem = m_listProducer.front();
            m_listProducer.pop_front();
        }
        return pElem;
    }

    case CONSUMER:
    {
        std::lock_guard<std::mutex> objLock(m_objConsumerLock);
        if (!m_listConsumer.empty())
        {
            pElem = m_listConsumer.front();
            m_listConsumer.pop_front();
        }
        return pElem;
    }

    default:
        return pElem;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   放入队列尾
\param   [in]    emType    队列类型
\param   [in]    pFrame    当前图像
\return  无
*/
//----------------------------------------------------------------------------------
void CQueueManager::PushBack(QUEUE_TYPE emType, std::shared_ptr<HV_FRAME_INFO> pFrame)
{
    switch (emType)
    {
    case PRODUCER:
    {
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        m_listProducer.push_back(pFrame);
    }
        break;
        
    case CONSUMER:
    {
        std::lock_guard<std::mutex> objLock(m_objConsumerLock);
        m_listConsumer.push_back(pFrame);
    }
        break;

    default:
        break;
    }
}

//----------------------------------------------------------------------------------
/**
\brief   拷贝当前图像并放入显示队列
\param   [in]    pImgData    当前图像
\return  无
*/
//----------------------------------------------------------------------------------
void CQueueManager::CopyToShow(CImageDataPointer pImgData)
{
    std::shared_ptr<HV_FRAME_INFO> pFrame = std::make_shared<HV_FRAME_INFO>();

    // 填充图像参数
    pFrame->nWidth = pImgData->GetWidth();
    pFrame->nHeight = pImgData->GetHeight();
    pFrame->nRawPixelFormat = pImgData->GetPixelFormat();
    pFrame->nImgBufferSize = pImgData->GetPayloadSize();
    pFrame->pBuffer = std::shared_ptr<uint8_t[]>(new uint8_t[pImgData->GetPayloadSize()]
        , std::default_delete<uint8_t[]>());

    // 图像拷贝
    memcpy(pFrame->pBuffer.get(), pImgData->GetBuffer(), pImgData->GetPayloadSize());

    // 放入显示队列尾
    std::lock_guard<std::mutex> objLock(m_objShowLock);
    m_listShow.push_back(pFrame);
}

//----------------------------------------------------------------------------------
/**
\brief   从生产队列取buffer进行拷贝，放入消费队列
\param   [in]    pImgData    当前图像
\return  无
*/
//----------------------------------------------------------------------------------
bool CQueueManager::CopyToConsumer(CImageDataPointer pImgData)
{
    std::shared_ptr<HV_FRAME_INFO> pFrame = nullptr;

    {
        std::lock_guard<std::mutex> objLock(m_objProducerLock);
        if (m_listProducer.empty())
        {
            // 当前申请buffer小于总buffer，并且生产队列已无buffer可用时，创建新buffer
            if (m_i32BufNum < m_i32TotalBufNum)
            {
                pFrame = std::make_shared<HV_FRAME_INFO>();
                pFrame->pBuffer = std::shared_ptr<uint8_t[]>(new uint8_t[m_ui64PayloadSize]
                    , std::default_delete<uint8_t[]>());
                m_i32BufNum++;
            }
            else
            {
                return false;
            }
        }
        else
        {
            pFrame = m_listProducer.front();
            m_listProducer.pop_front();
        }
    }

    pFrame->nWidth = pImgData->GetWidth();
    pFrame->nHeight = pImgData->GetHeight();
    pFrame->nRawPixelFormat = pImgData->GetPixelFormat();
    pFrame->nImgBufferSize = pImgData->GetPayloadSize();

    // 图像拷贝
    memcpy(pFrame->pBuffer.get(), pImgData->GetBuffer(), pImgData->GetPayloadSize());

    std::lock_guard<std::mutex> objLock(m_objConsumerLock);
    m_listConsumer.push_back(pFrame);
    
    return true;
}

//----------------------------------------------------------------------------------
/**
\brief   弹出显示队列头

\return  std::shared_ptr<HV_FRAME_INFO>    显示队列头图像
*/
//----------------------------------------------------------------------------------
std::shared_ptr<HV_FRAME_INFO> CQueueManager::PopShowlist()
{
    std::shared_ptr<HV_FRAME_INFO> pFrame = std::make_shared<HV_FRAME_INFO>();
    std::lock_guard<std::mutex> objLock(m_objShowLock);
    
    // 显示队列为空时返回空指针
    if (m_listShow.empty())
    {
        return nullptr;
    }

    // 弹出队列头图像
    pFrame = m_listShow.front();
    m_listShow.pop_front();
    return pFrame;
}

//----------------------------------------------------------------------------------
/**
\brief   等待条件变量触发
\param   [in]    ui32TimeOut    超时时间ms
\return  
*/
//----------------------------------------------------------------------------------
void CQueueManager::WaitFor(const uint32_t& ui32TimeOut)
{
    std::unique_lock<std::mutex> lock(m_objCVLock);
    m_objCV.wait_for(lock, std::chrono::milliseconds(ui32TimeOut));
}

//----------------------------------------------------------------------------------
/**
\brief   触发条件变量

\return
*/
//----------------------------------------------------------------------------------
void CQueueManager::Notify()
{
    m_objCV.notify_one();
}

//----------------------------------------------------------------------------------
/**
\brief   返回已申请buffer数

\return  m_i32BufNum        已申请buffer数
*/
//----------------------------------------------------------------------------------
uint32_t CQueueManager::AllocatedBufNum()
{
    return m_i32BufNum;
}