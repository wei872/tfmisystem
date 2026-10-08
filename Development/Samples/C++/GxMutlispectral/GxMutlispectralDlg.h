
// MutlispectralDlg.h: 头文件
//

#pragma once
#include "GxDeviceConfigDlg.h"
#include "GxOpenDeviceDlg.h"
#include "GxImageProcess.h"

#include <vector>
#include <queue>

// CMutlispectralDlg 对话框
class CGxMutlispectralDlg : public CDialogEx
{
// 构造
public:
	CGxMutlispectralDlg(CWnd* pParent = NULL);	// 标准构造函数
	~CGxMutlispectralDlg();

// 对话框数据
#ifdef AFX_DESIGN_TIME
	enum { IDD = IDD_MUTLISPECTRAL_DIALOG };
#endif

	protected:
	virtual void DoDataExchange(CDataExchange* pDX);	// DDX/DDV 支持

// 实现
protected:
	HICON m_hIcon;

	// 生成的消息映射函数
	virtual BOOL OnInitDialog();
	afx_msg void OnSysCommand(UINT nID, LPARAM lParam);
	afx_msg void OnPaint();
	afx_msg HCURSOR OnQueryDragIcon();
	DECLARE_MESSAGE_MAP()
public:
	virtual BOOL PreTranslateMessage(MSG* pMsg);
	afx_msg void OnNMThemeChangedScrollbar1(NMHDR *pNMHDR, LRESULT *pResult);
	afx_msg void OnClickedDeviceConfig();
	afx_msg void OnClickedOpenDevice();
	afx_msg void OnClickedAcquistionStart();
	afx_msg void OnClickedAcquistionStop();
	afx_msg void OnClickedCloseDevice();
	afx_msg void OnClickedBind1();
	afx_msg void OnClickedBind2();
	afx_msg void OnClickedBind3();
	afx_msg void OnClickedBind4();
	afx_msg void OnClickedSaveiamge();
	afx_msg LRESULT OnUpdateFrameUI(WPARAM wParam, LPARAM lParam);

public:
	/// 采集图像
	static DWORD WINAPI AcquireImage(LPVOID pParam);

	/// 图像处理
	static DWORD WINAPI ProcessImage(LPVOID pParam);

	/// 保存图像
	void SaveImage(const std::string & strFilePath, const std::vector<IMAGE_INFO>& vecImage);

	/// 图像显示
	void Display(std::vector<IMAGE_INFO>& vecImage);

	/// 更新UI使能状态
	void UpdateUI();

	/// 获取多光谱信息
	void GetMutliSpectralInfo(uint64_t& nROIHeight, std::vector<uint64_t>& vecGapValue, int& nBindsize);

	/// 是否支持多光谱
	bool IsSupportSpecturmControl();

	/// 更新Bind列表
	void UpdateBindList();

	/// 申请图像缓冲区
	void AllocImageBufferList();
	
	/// 清理图像缓冲区
	void ClearImageBufferList();

	/// 初始bind值
	void InitBind();

private:
	struct BIND_INFO {
		std::string strName;
		bool		bChecked;
		RECT		rect;
		CDC*		hdc;
	};

private:
	CGXDevicePointer				m_objDevicePtr;						///< 设备句柄
	CGXStreamPointer				m_objStreamPtr;                     ///< 流对象
	CGXFeatureControlPointer		m_objFeatureControlPtr;             ///< 属性控制器对象
	CGXFeatureControlPointer		m_objStreamFeatureControlPtr;       ///< 流层控制器对象
	bool							m_bSaveImage;						///< 是否保存图像
	uint64_t						m_nFrame;							///< 采集帧数
	uint64_t						m_nCurFrame;						///< 当前采集的帧数
	bool                            m_bIsOpen;							///< 设备打开标识
	bool                            m_bIsSnap;							///< 设备采集标识
	std::queue<std::vector<CImageDataPointer>> m_BufferQueue;			///< 采集的数据队列
	std::string						m_strFilePath;						///< 保存路径
	uint64_t						m_nROIHeight;						///< 多光谱ROI高度
	std::vector<uint64_t>			m_vecGapValue;						///< Gap值
	int								m_nBindSize;						///< Bind个数
	HANDLE 							m_hSnapThread;						///< 采集线程
	HANDLE							m_hDisplayThread;					///< 显示线程
	CMutex							m_mutex;							///< 锁
	std::vector<BIND_INFO>			m_vecBind;							///< Bind信息
	std::vector<IMAGE_INFO>			m_vecCacheBuffer;					///< 缓存buffer
};
