#pragma once

#include "GalaxyIncludes.h"

// COpenDevice 对话框

class CGxOpenDeviceDlg : public CDialog
{
	DECLARE_DYNAMIC(CGxOpenDeviceDlg)

public:
	CGxOpenDeviceDlg(CWnd* pParent = NULL);   // 标准构造函数
	virtual ~CGxOpenDeviceDlg();

// 对话框数据
#ifdef AFX_DESIGN_TIME
	enum { IDD = IDD_DIALOG1 };
#endif

	GxIAPICPP::gxstring GetDeviceSN();

	afx_msg void OnOK();
protected:
	virtual BOOL OnInitDialog();

	virtual void DoDataExchange(CDataExchange* pDX);    // DDX/DDV 支持

	DECLARE_MESSAGE_MAP()
private:
	CComboBox						m_comboChooseDevice;
	GxIAPICPP::gxdeviceinfo_vector	m_vectorDeviceInfo;
	int								m_nCurIndex;
};
