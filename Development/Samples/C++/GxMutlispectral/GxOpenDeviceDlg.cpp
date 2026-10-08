// OpenDevice.cpp: 实现文件
//

#include "stdafx.h"
#include "GxMutlispectral.h"
#include "GxOpenDeviceDlg.h"
#include "afxdialogex.h"



// COpenDevice 对话框

IMPLEMENT_DYNAMIC(CGxOpenDeviceDlg, CDialogEx)

CGxOpenDeviceDlg::CGxOpenDeviceDlg(CWnd* pParent /*=NULL*/)
	: CDialog(IDD_OPENDEVICEDLG, pParent), m_nCurIndex(-1)
{
}

CGxOpenDeviceDlg::~CGxOpenDeviceDlg()
{
}

GxIAPICPP::gxstring CGxOpenDeviceDlg::GetDeviceSN()
{
	return m_vectorDeviceInfo[m_nCurIndex].GetSN();
}

void CGxOpenDeviceDlg::OnOK()
{
	m_nCurIndex = m_comboChooseDevice.GetCurSel();
	CDialog::OnOK();
}

BOOL CGxOpenDeviceDlg::OnInitDialog()
{
	CDialog::OnInitDialog();

	m_comboChooseDevice.ResetContent();

	try
	{
		//枚举相机设备
		IGXFactory::GetInstance().UpdateAllDeviceList(1000, m_vectorDeviceInfo);
		if (m_vectorDeviceInfo.size() <= 0)
		{
			m_comboChooseDevice.SetCurSel(-1);
		}
		else
		{
			for (int i = 0; i < m_vectorDeviceInfo.size(); ++i)
			{
				std::string str = m_vectorDeviceInfo[i].GetDisplayName();
				m_comboChooseDevice.AddString(m_vectorDeviceInfo[i].GetDisplayName());
			}
			// 默认下拉列表选择第一台相机
			m_comboChooseDevice.SetCurSel(0);
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

	return TRUE;
}

void CGxOpenDeviceDlg::DoDataExchange(CDataExchange* pDX)
{
	CDialog::DoDataExchange(pDX);
	DDX_Control(pDX, IDC_DEVICELIST, m_comboChooseDevice);
}


BEGIN_MESSAGE_MAP(CGxOpenDeviceDlg, CDialog)
END_MESSAGE_MAP()


// COpenDevice 消息处理程序
