#pragma once
#include <list>
#include <memory>
#include <mutex>
#include <atomic>
#include <GalaxyIncludes.h>

#define  INIT_BUF_NUM                   5      ///< 初始分配的buffer数量

// 队列类型
typedef enum QUEUE_TYPE
{
    PRODUCER,   ///< 采集队列
    CONSUMER,   ///< 消费队列
}QUEUE_TYPE;

typedef struct HV_FRAME_INFO
{
public:
    // 构造函数
    HV_FRAME_INFO()
        :nImgBufferSize(0)
        , nWidth(0)
        , nHeight(0)
        , nRawPixelFormat(0)
    {}

    // 拷贝构造
    HV_FRAME_INFO(const HV_FRAME_INFO& stAnother)
    {
        nImgBufferSize = stAnother.nImgBufferSize;
        nWidth = stAnother.nWidth;
        nHeight = stAnother.nHeight;
        nRawPixelFormat = stAnother.nRawPixelFormat;
    }

    // = 重载
    HV_FRAME_INFO& operator= (const HV_FRAME_INFO& stAnother)
    {
        if (this != &stAnother)
        {
            nImgBufferSize = stAnother.nImgBufferSize;
            nWidth = stAnother.nWidth;
            nHeight = stAnother.nHeight;
            nRawPixelFormat = stAnother.nRawPixelFormat;
        }

        return *this;
    }

    // == 重载
    bool operator == (const HV_FRAME_INFO& stAnother) const
    {
        if ((nImgBufferSize == stAnother.nImgBufferSize)
            && (nWidth == stAnother.nWidth)
            && (nHeight == stAnother.nHeight)
            && (nRawPixelFormat == stAnother.nRawPixelFormat))
        {
            return true;
        }
        else
        {
            return false;
        }
    }

public:
    size_t                     nImgBufferSize;   ///< 图像数据的字节数(即PayloadSize)
    size_t                     nWidth;           ///< 图像的宽
    size_t                     nHeight;          ///< 图像的高
    int32_t                    nRawPixelFormat;  ///< 图像的像素格式
    std::shared_ptr<uint8_t[]> pBuffer;          ///< 图像指针
}HV_FRAME_INFO;

class CQueueManager
{
public:
    // 构造函数
    CQueueManager();

    // 析构函数
    ~CQueueManager();

    // 初始化采集buffer并保存
    void InitQueue(const int32_t& i32TotalBufNum
        , const uint64_t& ui64PayloadSize);

    // 反初始化队列
    void UnInitQueue();

    // 重置内存
    void ResetMem();

    // 获取队列大小
    uint32_t GetElemSize(QUEUE_TYPE emType);

    // 弹出队列头
    std::shared_ptr<HV_FRAME_INFO> PopFront(QUEUE_TYPE emType);

    // 放入队列尾
    void PushBack(QUEUE_TYPE emType, std::shared_ptr<HV_FRAME_INFO> pFrame);

    // 拷贝图像到显示队列
    void CopyToShow(CImageDataPointer pImgData);

    // 拷贝图像到消费队列
    bool CopyToConsumer(CImageDataPointer pImgData);

    // 弹出显示队列头
    std::shared_ptr<HV_FRAME_INFO> PopShowlist();

    // 等待条件变量触发
    void WaitFor(const uint32_t& ui32TimeOut);

    // 触发条件变量
    void Notify();

    // 已申请buffer数
    uint32_t AllocatedBufNum();

private:
    std::mutex                                  m_objProducerLock;  ///< 生产者队列锁
    std::mutex                                  m_objConsumerLock;  ///< 录像存图队列锁
    std::mutex                                  m_objShowLock;      ///< 显示队列锁
    std::list<std::shared_ptr<HV_FRAME_INFO>>   m_listProducer;     ///< 生产者队列
    std::list<std::shared_ptr<HV_FRAME_INFO>>   m_listConsumer;     ///< 录像存图队列
    std::list<std::shared_ptr<HV_FRAME_INFO>>   m_listShow;         ///< 显示队列
    uint64_t                                    m_ui64PayloadSize;  ///< 图像大小
    std::mutex                                  m_objCVLock;        ///< 条件变量锁
    std::condition_variable                     m_objCV;            ///< 条件变量
    std::atomic_int                             m_i32BufNum;        ///< 实际申请buffer数量
    std::int32_t                                m_i32TotalBufNum;   ///< 理论申请buffer数量
    bool                                        m_bInit;            ///< 已经初始化队列
};

