
// MutlispectralDlg.cpp: 实现文件
//

#include "stdafx.h"
#include "framework.h"
#include "GxMutlispectral.h"
#include "GxMutlispectralDlg.h"
#include "afxdialogex.h"
#include <windows.h>
#include <sstream>

#ifdef _DEBUG
#define new DEBUG_NEW
#endif

#define CACHE_LENGTH 3

#define WM_UPDATEFRAME_UI (WM_USER + 100)

// 用于应用程序“关于”菜单项的 CAboutDlg 对话框

class CAboutDlg : public CDialog
{
public:
	CAboutDlg();

// 对话框数据
#ifdef AFX_DESIGN_TIME
	enum { IDD = IDD_ABOUTBOX };
#endif

	protected:
	virtual void DoDataExchange(CDataExchange* pDX);    // DDX/DDV 支持

// 实现
protected:
	DECLARE_MESSAGE_MAP()
};

CAboutDlg::CAboutDlg() : CDialog(IDD_ABOUTBOX)
{
}

void CAboutDlg::DoDataExchange(CDataExchange* pDX)
{
	CDialog::DoDataExchange(pDX);
}

BEGIN_MESSAGE_MAP(CAboutDlg, CDialog)
END_MESSAGE_MAP()


// CMutlispectralDlg 对话框
CGxMutlispectralDlg::CGxMutlispectralDlg(CWnd* pParent /*=NULL*/)
	: CDialogEx(IDD_MUTLISPECTRAL_DIALOG, pParent), m_objDevicePtr(NULL)
	, m_bSaveImage(false)
	, m_nFrame(0)
	, m_nCurFrame(0)
	, m_bIsOpen(false)
	, m_bIsSnap(false)
{
	m_hIcon = AfxGetApp()->LoadIcon(IDR_MAINFRAME);

	//获取可执行程序的当前路径
	char    strFileName[MAX_PATH] = { 0 };
	size_t  nPos = 0;
	GetModuleFileName(NULL, (LPCH)strFileName, MAX_PATH);
	m_strFilePath = strFileName;
	nPos = m_strFilePath.find_last_of('\\');
	m_strFilePath = m_strFilePath.substr(0, nPos);
	m_strFilePath = m_strFilePath + "\\Images";

	//初始化设备库
	IGXFactory::GetInstance().Init();
}

CGxMutlispectralDlg::~CGxMutlispectralDlg()
{
	//释放设备资源
	IGXFactory::GetInstance().Uninit();
}

void CGxMutlispectralDlg::DoDataExchange(CDataExchange* pDX)
{
	CDialogEx::DoDataExchange(pDX);
	DDX_Text(pDX, IDC_CURFRAME, m_nCurFrame);
}

BEGIN_MESSAGE_MAP(CGxMutlispectralDlg, CDialogEx)
	ON_WM_SYSCOMMAND()
	ON_WM_PAINT()
	ON_WM_QUERYDRAGICON()
	ON_BN_CLICKED(IDC_DEVICECONFIG, &CGxMutlispectralDlg::OnClickedDeviceConfig)
	ON_BN_CLICKED(IDC_OPENDEVICE, &CGxMutlispectralDlg::OnClickedOpenDevice)
	ON_BN_CLICKED(IDC_ACQUISTIONSTART, &CGxMutlispectralDlg::OnClickedAcquistionStart)
	ON_BN_CLICKED(IDC_ACQUISTIONSTOP, &CGxMutlispectralDlg::OnClickedAcquistionStop)
	ON_BN_CLICKED(IDC_CLOSEDEVICE, &CGxMutlispectralDlg::OnClickedCloseDevice)
	ON_BN_CLICKED(IDC_BIND1, &CGxMutlispectralDlg::OnClickedBind1)
	ON_BN_CLICKED(IDC_BIND2, &CGxMutlispectralDlg::OnClickedBind2)
	ON_BN_CLICKED(IDC_BIND3, &CGxMutlispectralDlg::OnClickedBind3)
	ON_BN_CLICKED(IDC_BIND4, &CGxMutlispectralDlg::OnClickedBind4)
	ON_BN_CLICKED(IDC_SAVEIMAGE, &CGxMutlispectralDlg::OnClickedSaveiamge)
	ON_MESSAGE(WM_UPDATEFRAME_UI, &CGxMutlispectralDlg::OnUpdateFrameUI)
END_MESSAGE_MAP()


BOOL CGxMutlispectralDlg::OnInitDialog()
{
	CDialogEx::OnInitDialog();

	// 将“关于...”菜单项添加到系统菜单中。

	// IDM_ABOUTBOX 必须在系统命令范围内。
	ASSERT((IDM_ABOUTBOX & 0xFFF0) == IDM_ABOUTBOX);
	ASSERT(IDM_ABOUTBOX < 0xF000);

	CMenu* pSysMenu = GetSystemMenu(FALSE);
	if (pSysMenu != NULL)
	{
		BOOL bNameValid;
		CString strAboutMenu;
		bNameValid = strAboutMenu.LoadString(IDS_ABOUTBOX);
		ASSERT(bNameValid);
		if (!strAboutMenu.IsEmpty())
		{
			pSysMenu->AppendMenu(MF_SEPARATOR);
			pSysMenu->AppendMenu(MF_STRING, IDM_ABOUTBOX, strAboutMenu);
		}
	}

	// 设置此对话框的图标。  当应用程序主窗口不是对话框时，框架将自动
	//  执行此操作
	SetIcon(m_hIcon, TRUE);			// 设置大图标
	SetIcon(m_hIcon, FALSE);		// 设置小图标

	m_nFrame = 50;
	GetDlgItem(IDC_EDITFRAME)->SetWindowText(std::to_string(m_nFrame).c_str());

	//更新UI
	UpdateUI();

	return TRUE;  // 除非将焦点设置到控件，否则返回 TRUE
}

void CGxMutlispectralDlg::OnSysCommand(UINT nID, LPARAM lParam)
{
	if ((nID & 0xFFF0) == IDM_ABOUTBOX)
	{
		CAboutDlg dlgAbout;
		dlgAbout.DoModal();
	}
	else
	{
		CDialogEx::OnSysCommand(nID, lParam);
	}
}

// 如果向对话框添加最小化按钮，则需要下面的代码
//  来绘制该图标。  对于使用文档/视图模型的 MFC 应用程序，
//  这将由框架自动完成。

void CGxMutlispectralDlg::OnPaint()
{
	if (IsIconic())
	{
		CPaintDC dc(this); // 用于绘制的设备上下文

		SendMessage(WM_ICONERASEBKGND, reinterpret_cast<WPARAM>(dc.GetSafeHdc()), 0);

		// 使图标在工作区矩形中居中
		int cxIcon = GetSystemMetrics(SM_CXICON);
		int cyIcon = GetSystemMetrics(SM_CYICON);
		CRect rect;
		GetClientRect(&rect);
		int x = (rect.Width() - cxIcon + 1) / 2;
		int y = (rect.Height() - cyIcon + 1) / 2;

		// 绘制图标
		dc.DrawIcon(x, y, m_hIcon);
	}
	else
	{
		CDialogEx::OnPaint();
	}
}

//当用户拖动最小化窗口时系统调用此函数取得光标
//显示。
HCURSOR CGxMutlispectralDlg::OnQueryDragIcon()
{
	return static_cast<HCURSOR>(m_hIcon);
}

void CGxMutlispectralDlg::OnNMThemeChangedScrollbar1(NMHDR *pNMHDR, LRESULT *pResult)
{
	// 该功能要求使用 Windows XP 或更高版本。
	// 符号 _WIN32_WINNT 必须 >= 0x0501。
	// TODO: 在此添加控件通知处理程序代码
	*pResult = 0;
}


BOOL CGxMutlispectralDlg::PreTranslateMessage(MSG * pMsg)
{
	CWnd  *pWnd = NULL;
	int   nCtrlID = 0;             //< 保存获取的控件ID

	//判断是否是键盘回车消息
	if ((pMsg->message == WM_KEYDOWN) && (pMsg->wParam == VK_RETURN))
	{
		//获取当前拥有输入焦点的窗口(控件)指针
		pWnd = GetFocus();

		//获得当前焦点的控件ID
		nCtrlID = pWnd->GetDlgCtrlID();

		//判断ID类型
		switch (nCtrlID)
		{
		case IDC_EDITFRAME:
			//失去焦点
			SetFocus();

			break;

		default:
			break;
		}

		return TRUE;
	}
	if ((pMsg->message == WM_KEYDOWN) && (pMsg->wParam == VK_ESCAPE))
	{
		return  TRUE;
	}

	return CDialog::PreTranslateMessage(pMsg);
}

void CGxMutlispectralDlg::OnClickedDeviceConfig()
{
	CGxDeviceConfigDlg cfg(m_objFeatureControlPtr, m_bIsSnap);
	cfg.DoModal();
}

void CGxMutlispectralDlg::OnClickedOpenDevice()
{
	CGxOpenDeviceDlg dlg;
	INT_PTR nResult = dlg.DoModal();
	if (nResult != IDOK) {
		return;
	}

	try
	{
		//通过SN打开相机设备
		m_objDevicePtr = IGXFactory::GetInstance().OpenDeviceBySN(dlg.GetDeviceSN(), GX_ACCESS_EXCLUSIVE);
		m_objFeatureControlPtr = m_objDevicePtr->GetRemoteFeatureControl();
		//获取流通道个数
		int32_t nStreamCount = m_objDevicePtr->GetStreamCount();
		if (nStreamCount > 0)
		{
			m_objStreamPtr = m_objDevicePtr->OpenStream(0);
			m_objStreamFeatureControlPtr = m_objStreamPtr->GetFeatureControl();
		}
		else
		{
			m_objDevicePtr->Close();
			MessageBox(_T("未找到设备流!"));
		}

		//不支持则弹框提示并关闭设备
		if (!IsSupportSpecturmControl())
		{
			m_objStreamPtr->Close();
			m_objDevicePtr->Close();
			MessageBox(_T("当前相机不支持多光谱!"));
			return;
		}
		
		//确保SensorOutputROIMode在Band模式下，其他模式子不支持
		m_objFeatureControlPtr->GetEnumFeature("SensorOutputROIMode")->SetValue("Band");

		m_bIsOpen = true;
		UpdateUI();
		InitBind();
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedAcquistionStart()
{
	CString strValue;
	GetDlgItem(IDC_EDITFRAME)->GetWindowText(strValue);
	m_nFrame = _ttoi(strValue);

	if (m_nFrame > 300)
	{
		MessageBox(_T("此程序仅为示例，处理过大的图像可能会导致其他性能问题！"));
	}

	if (m_nFrame == 0)
	{
		MessageBox(_T("帧号不能为空!"));
		return;
	}

	try
	{
		//获取多光谱信息
		GetMutliSpectralInfo(m_nROIHeight, m_vecGapValue, m_nBindSize);
		if (m_nBindSize == 0)
		{
			MessageBox(_T("Bind不能全不勾选!"));
			return;
		}

		if ((!m_vecGapValue.empty()) && m_nFrame*m_nROIHeight < m_vecGapValue.back())
		{
			MessageBox(_T("设置的Frame值小于Gap偏移量!"));
			return;
		}

		m_nCurFrame = 0;
		std::string strCurFame = std::to_string(m_nCurFrame);
		GetDlgItem(IDC_CURFRAME)->SetWindowText(strCurFame.c_str());

		//设置采集的buffer大小
		m_objStreamPtr->SetAcqusitionBufferNumber(m_nFrame*CACHE_LENGTH);
		
		//分配缓存buffer列表
		AllocImageBufferList();
	
		//在发送开采命令前必须先开启流层采集
		m_objStreamPtr->StartGrab();

		//获取一个命令型控制器并发送开采命令
		m_objFeatureControlPtr->GetCommandFeature("AcquisitionStart")->Execute();

		m_bIsSnap = true;
		UpdateUI();
		UpdateBindList();

		//开启采集和显示线程
		m_hSnapThread = ::CreateThread(NULL, 0, CGxMutlispectralDlg::AcquireImage, this, 0 , NULL);
		m_hDisplayThread = ::CreateThread(NULL, 0, CGxMutlispectralDlg::ProcessImage, this, 0, NULL);
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedAcquistionStop()
{
	try
	{
		if (m_bIsSnap)
		{
			m_bIsSnap = false;
			WaitForSingleObject(m_hSnapThread, INFINITE);
			WaitForSingleObject(m_hDisplayThread, INFINITE);

			m_objFeatureControlPtr->GetCommandFeature("AcquisitionStop")->Execute();
			//清理buffer缓存列表
			ClearImageBufferList();
			m_objStreamPtr->StopGrab();

			UpdateUI();
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedCloseDevice()
{
	try
	{
		//停止采集
		OnClickedAcquistionStop();

		//关闭设备
		if (m_bIsOpen)
		{
			m_objStreamPtr->Close();
			m_objDevicePtr->Close();

			m_bIsOpen = false;
			UpdateUI();
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedBind1()
{
	try
	{
		CButton * pButton = (CButton *)GetDlgItem(IDC_BIND1);
		if (m_objFeatureControlPtr->IsImplemented("SpectrumEnable") &&
			m_objFeatureControlPtr->IsWritable("SpectrumEnable"))
		{
			m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band1");
			m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->SetValue((bool)pButton->GetCheck());
		}
		else
		{
			CString strMessage;
			strMessage.Format(_T("<%s>"), _T("节点不存在或不可写!"));
			MessageBox(strMessage);
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}

}

void CGxMutlispectralDlg::OnClickedBind2()
{
	try
	{
		CButton * pButton = (CButton *)GetDlgItem(IDC_BIND2);
		if (m_objFeatureControlPtr->IsImplemented("SpectrumEnable") &&
			m_objFeatureControlPtr->IsWritable("SpectrumEnable"))
		{
			m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band2");
			m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->SetValue((bool)pButton->GetCheck());
		}
		else
		{
			CString strMessage;
			strMessage.Format(_T("<%s>"), _T("节点不存在或不可写!"));
			MessageBox(strMessage);
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedBind3()
{
	try
	{
		CButton * pButton = (CButton *)GetDlgItem(IDC_BIND3);
		if (m_objFeatureControlPtr->IsImplemented("SpectrumEnable") &&
			m_objFeatureControlPtr->IsWritable("SpectrumEnable"))
		{
			m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band3");
			m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->SetValue((bool)pButton->GetCheck());
		}
		else
		{
			CString strMessage;
			strMessage.Format(_T("<%s>"), _T("节点不存在或不可写!"));
			MessageBox(strMessage);
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedBind4()
{
	try
	{
		CButton * pButton = (CButton *)GetDlgItem(IDC_BIND4);
		if (m_objFeatureControlPtr->IsImplemented("SpectrumEnable") &&
			m_objFeatureControlPtr->IsWritable("SpectrumEnable"))
		{
			m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band4");
			m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->SetValue((bool)pButton->GetCheck());
		}
		else
		{
			CString strMessage;
			strMessage.Format(_T("<%s>"), _T("节点不存在或不可写!"));
			MessageBox(strMessage);
		}
	}
	catch (CGalaxyException &e)
	{
		MessageBox(e.what());
	}
	catch (std::exception &e)
	{
		MessageBox(e.what());
	}
}

void CGxMutlispectralDlg::OnClickedSaveiamge()
{
	CButton * pButton = (CButton *)GetDlgItem(IDC_SAVEIMAGE);
	m_bSaveImage = pButton->GetCheck();
}

LRESULT CGxMutlispectralDlg::OnUpdateFrameUI(WPARAM wParam, LPARAM lParam)
{
	uint64_t* pFrame = reinterpret_cast<uint64_t*>(lParam);
	std::string strCurFame = std::to_string(*pFrame);
	GetDlgItem(IDC_CURFRAME)->SetWindowText(strCurFame.c_str());
	return 0;
}

//---------------------------------------------------------------------------------
/**
\brief   采集图像， 每次采集m_nFrame张图像然后加入队列

\return  无
*/
//----------------------------------------------------------------------------------
DWORD WINAPI CGxMutlispectralDlg::AcquireImage(LPVOID pParam)
{
	CGxMutlispectralDlg* dlg = (CGxMutlispectralDlg*)pParam;
	while (dlg->m_bIsSnap)
	{
		{
			dlg->m_mutex.Lock();
			//缓存数据超过默认长度时等待处理完成后再次采集，此时采集可能会出现丢帧
			size_t nSize = dlg->m_BufferQueue.size();
			if (nSize > CACHE_LENGTH)
			{
				dlg->m_mutex.Unlock();
				continue;
			}
			dlg->m_mutex.Unlock();
		}

		std::vector<CImageDataPointer>  vecImage;
		for (int i = 0; i < dlg->m_nFrame; ++i)
		{
			try
			{
				if (!dlg->m_bIsSnap)
				{
					break;
				}

				CImageDataPointer pImage = dlg->m_objStreamPtr->DQBuf(300);
				vecImage.emplace_back(pImage);

				dlg->m_nCurFrame++;
				::PostMessage(dlg->GetSafeHwnd(), WM_UPDATEFRAME_UI, 0, (LPARAM)&dlg->m_nCurFrame);

			}
			catch (CGalaxyException &e)
			{
				i--;
			}
			catch (std::exception &e)
			{
			}
		}

		{
			dlg->m_mutex.Lock();
			dlg->m_BufferQueue.emplace(vecImage);
			dlg->m_mutex.Unlock();
		}
	}

	return 0;
}

//---------------------------------------------------------------------------------
/**
\brief   采集图像， 每次采集m_nFrame张图像然后加入队列

\return  无
*/
//----------------------------------------------------------------------------------
DWORD WINAPI CGxMutlispectralDlg::ProcessImage(LPVOID pParam)
{
	CGxMutlispectralDlg* dlg = (CGxMutlispectralDlg*)pParam;
	while (dlg->m_bIsSnap)
	{
		std::vector<CImageDataPointer> vecImage;
		int nSize = 0;
		{
			dlg->m_mutex.Lock();
			nSize = dlg->m_BufferQueue.size();
			dlg->m_mutex.Unlock();
		}

		if (nSize == 0)
		{
			Sleep(100);
			continue;
		}

		{
			dlg->m_mutex.Lock();
			vecImage = dlg->m_BufferQueue.front();
			dlg->m_mutex.Unlock();
		}

		do
		{	
			//图像拆分重组
			if (!CGxImageProcess::DivideImage(vecImage, dlg->m_nROIHeight, dlg->m_vecCacheBuffer))
			{
				break;
			}

			std::vector<IMAGE_INFO> vecDisplayImage;
			//图像对齐
			if (!CGxImageProcess::MatchAndAlign(dlg->m_vecCacheBuffer, dlg->m_vecGapValue, vecDisplayImage))
			{
				break;
			}

			//显示图像
			dlg->Display(vecDisplayImage);

			//保存图像
			if (dlg->m_bSaveImage)
			{
				dlg->SaveImage(dlg->m_strFilePath, vecDisplayImage);
			}
				
		} while (0);

		try
		{
			//归还buffer
			for (int i=0; i< vecImage.size(); ++i)
			{
				dlg->m_objStreamPtr->QBuf(vecImage[i]);
			}
		}
		catch (CGalaxyException &e)
		{
		}
		catch (std::exception &e)
		{
		}

		//出队
		{
			dlg->m_mutex.Lock();
			dlg->m_BufferQueue.pop();
			dlg->m_mutex.Unlock();
		}
	}
	return 0;
}

//---------------------------------------------------------------------------------
/**
\brief   保存图像， 每次采集m_nFrame张图像然后加入队列
\param   [in]strFilePath		保存文件夹路径
\param   [in]vecDisplayImage	保存文件夹路径
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::SaveImage(const std::string & strFilePath, const std::vector<IMAGE_INFO>& vecImage)
{
	for (int i = 0; i < vecImage.size(); ++i)
	{
		SYSTEMTIME   sysTime;                   ///< 系统时间
		CString      strFileName = "";          ///< 图像保存路径名称
		//获取当前时间为图像保存的默认名称
		GetLocalTime(&sysTime);
		strFileName.Format("%s\\%s-%d_%d_%d_%d_%d_%d_%d.bmp", strFilePath.c_str(),
			vecImage[i].strBindName.c_str(),
			sysTime.wYear,
			sysTime.wMonth,
			sysTime.wDay,
			sysTime.wHour,
			sysTime.wMinute,
			sysTime.wSecond,
			sysTime.wMilliseconds);

		CGxImageProcess::SaveRGB24ToBMP(strFileName, vecImage[i].pImage, vecImage[i].nWidth, vecImage[i].nHeight);
	}
}

//---------------------------------------------------------------------------------
/**
\brief   图像显示
\param   [in]vecImage		图像信息列表
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::Display(std::vector<IMAGE_INFO>& vecImage)
{
	std::vector<BIND_INFO> vecTemp = m_vecBind;
	for (int i = 0; i < vecImage.size(); ++i)
	{
		std::vector<BIND_INFO>::iterator stBindInfo = vecTemp.begin();
		for ( ; stBindInfo != vecTemp.end();)
		{
			if (stBindInfo->bChecked)
			{
				CGxImageProcess::DisplayRGB24Image(stBindInfo->hdc, stBindInfo->rect, vecImage[i].pImage, vecImage[i].nWidth, vecImage[i].nHeight);
				vecImage[i].strBindName = stBindInfo->strName;
				vecTemp.erase(stBindInfo);
				break;
			}

			stBindInfo++;
		}
	}
}

//---------------------------------------------------------------------------------
/**
\brief   更新UI使能状态
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::UpdateUI()
{
	GetDlgItem(IDC_OPENDEVICE)->EnableWindow(!m_bIsOpen);
	GetDlgItem(IDC_ACQUISTIONSTART)->EnableWindow(m_bIsOpen && !m_bIsSnap);
	GetDlgItem(IDC_ACQUISTIONSTOP)->EnableWindow(m_bIsOpen && m_bIsSnap);
	GetDlgItem(IDC_DEVICECONFIG)->EnableWindow(m_bIsOpen);
	GetDlgItem(IDC_CLOSEDEVICE)->EnableWindow(m_bIsOpen);
	GetDlgItem(IDC_EDITFRAME)->EnableWindow(!m_bIsSnap); 
	GetDlgItem(IDC_SAVEIMAGE)->EnableWindow(!m_bIsSnap);
	GetDlgItem(IDC_BIND1)->EnableWindow(m_bIsOpen && !m_bIsSnap);
	GetDlgItem(IDC_BIND2)->EnableWindow(m_bIsOpen && !m_bIsSnap);
	GetDlgItem(IDC_BIND3)->EnableWindow(m_bIsOpen && !m_bIsSnap);
	GetDlgItem(IDC_BIND4)->EnableWindow(m_bIsOpen && !m_bIsSnap); 
}

//---------------------------------------------------------------------------------
/**
\brief   获取多光谱信息
\param   [in]nROIHeight		ROI高度
\param   [in]vecGapValue	GAP列表
\param   [in]nBindsize		Bind个数
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::GetMutliSpectralInfo(uint64_t & nROIHeight, std::vector<uint64_t>& vecGapValue, int & nBindsize)
{
	nROIHeight = m_objFeatureControlPtr->GetIntFeature("SpectrumROIHeight")->GetValue();
	nBindsize = (int)m_objFeatureControlPtr->GetIntFeature("SpectrumEnableValueAllUsedStatus")->GetValue();

	if (nBindsize <= 1)
	{
		return;
	}

	//获取Gap偏移值
	vecGapValue.clear();
	for (int i=0; i< nBindsize-1; ++i)
	{
		std::ostringstream oss;
		oss << i+1;
		std::string strName = "Gap" + oss.str();
		m_objFeatureControlPtr->GetEnumFeature("SpectrumBandGapSelector")->SetValue(strName.c_str());
		uint64_t nGapValue = m_objFeatureControlPtr->GetIntFeature("SpectrumBandGapValue")->GetValue();
		vecGapValue.emplace_back(nGapValue);
	}

}

//---------------------------------------------------------------------------------
/**
\brief   是否支持多光谱
\return  是否支持
*/
//----------------------------------------------------------------------------------
bool CGxMutlispectralDlg::IsSupportSpecturmControl()
{
	return m_objFeatureControlPtr->IsImplemented("SensorOutputROIMode");
}

//---------------------------------------------------------------------------------
/**
\brief   更新Bind列表
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::UpdateBindList()
{
	CButton* m_pWnd1 = (CButton *)GetDlgItem(IDC_BIND1);
	CButton* m_pWnd2 = (CButton *)GetDlgItem(IDC_BIND2);
	CButton* m_pWnd3 = (CButton *)GetDlgItem(IDC_BIND3);
	CButton* m_pWnd4 = (CButton *)GetDlgItem(IDC_BIND4);

	m_vecBind.clear();
	CString strValue;
	RECT rect;
	m_pWnd1->GetWindowText(strValue);
	std::string strBind1 = strValue.GetString();
	CStatic* pImg = (CStatic*)GetDlgItem(IDC_IMAGE1);
	pImg->GetClientRect(&rect);
	BIND_INFO stBind1;
	stBind1.strName = strBind1;
	stBind1.bChecked = m_pWnd1->GetCheck();
	stBind1.rect = rect;
	stBind1.hdc = pImg->GetDC();
	m_vecBind.push_back(stBind1);
	pImg->SetBitmap(NULL);
	pImg->ShowWindow(false);
	pImg->ShowWindow(true);

	m_pWnd2->GetWindowText(strValue);
	std::string strBind2 = strValue.GetString();
	pImg = (CStatic*)GetDlgItem(IDC_IMAGE2);
	pImg->GetClientRect(&rect);
	BIND_INFO stBind2;
	stBind2.strName = strBind2;
	stBind2.bChecked = m_pWnd2->GetCheck();
	stBind2.rect = rect;
	stBind2.hdc = pImg->GetDC();
	m_vecBind.push_back(stBind2);
	pImg->SetBitmap(NULL);
	pImg->ShowWindow(false);
	pImg->ShowWindow(true);

	m_pWnd3->GetWindowText(strValue);
	std::string strBind3 = strValue.GetString();
	pImg = (CStatic*)GetDlgItem(IDC_IMAGE3);
	pImg->GetClientRect(&rect);
	BIND_INFO stBind3;
	stBind3.strName = strBind3;
	stBind3.bChecked = m_pWnd3->GetCheck();
	stBind3.rect = rect;
	stBind3.hdc = pImg->GetDC();
	m_vecBind.push_back(stBind3);
	pImg->SetBitmap(NULL);
	pImg->ShowWindow(false);
	pImg->ShowWindow(true);

	m_pWnd4->GetWindowText(strValue);
	std::string strBind4 = strValue.GetString();
	pImg = (CStatic*)GetDlgItem(IDC_IMAGE4);
	pImg->GetClientRect(&rect);
	BIND_INFO stBind4;
	stBind4.strName = strBind4;
	stBind4.bChecked = m_pWnd4->GetCheck();
	stBind4.rect = rect;
	stBind4.hdc = pImg->GetDC();
	m_vecBind.push_back(stBind4);
	pImg->SetBitmap(NULL);
	pImg->ShowWindow(false);
	pImg->ShowWindow(true);
}

//---------------------------------------------------------------------------------
/**
\brief   申请图像缓冲区
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::AllocImageBufferList()
{
	const int64_t PIXEL = 3;
	uint64_t nWidth = m_objFeatureControlPtr->GetIntFeature("Width")->GetValue();
	uint64_t nNewImageHeight = m_nROIHeight * m_nFrame;

	m_vecCacheBuffer.resize(m_nBindSize);
	for (int i = 0; i < m_nBindSize; ++i) {
		char* newImage = new char[nWidth*nNewImageHeight*PIXEL];
		IMAGE_INFO stInfo;
		stInfo.pImage = newImage;
		stInfo.nWidth = nWidth;
		stInfo.nHeight = nNewImageHeight;
		m_vecCacheBuffer[i] = stInfo;
	}
}

//---------------------------------------------------------------------------------
/**
\brief   清理图像缓冲区
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::ClearImageBufferList()
{
	m_mutex.Lock();

	while (!m_BufferQueue.empty())
	{
		std::vector<CImageDataPointer> vecData = m_BufferQueue.front();
		for (int i = 0; i < vecData.size(); ++i)
		{
			m_objStreamPtr->QBuf(vecData[i]);
		}
		m_BufferQueue.pop();
	}

	for (int i = 0; i < m_vecCacheBuffer.size(); ++i)
	{
		delete[] m_vecCacheBuffer[i].pImage;
	}
	m_vecCacheBuffer.clear();
	m_mutex.Unlock();
}

//---------------------------------------------------------------------------------
/**
\brief   初始化Bind值
\return  无
*/
//----------------------------------------------------------------------------------
void CGxMutlispectralDlg::InitBind()
{
	m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band1");
	bool bBand1 = m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->GetValue();
	m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band2");
	bool bBand2 = m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->GetValue();
	m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band3");
	bool bBand3 = m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->GetValue();
	m_objFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue("Band4");
	bool bBand4 = m_objFeatureControlPtr->GetBoolFeature("SpectrumEnable")->GetValue();

	((CButton*)GetDlgItem(IDC_BIND1))->SetCheck(bBand1);
	((CButton*)GetDlgItem(IDC_BIND2))->SetCheck(bBand2);
	((CButton*)GetDlgItem(IDC_BIND3))->SetCheck(bBand3);
	((CButton*)GetDlgItem(IDC_BIND4))->SetCheck(bBand4);
}

