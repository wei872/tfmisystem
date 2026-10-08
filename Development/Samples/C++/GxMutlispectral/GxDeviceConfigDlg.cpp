// CDeviceConfig.cpp: 实现文件
//

#include "stdafx.h"
#include "GxMutlispectral.h"
#include "GxDeviceConfigDlg.h"
#include "afxdialogex.h"
#include <iostream>
#include <string>
#include <cstring>
#include <sstream>

// CDeviceConfig 对话框

IMPLEMENT_DYNAMIC(CGxDeviceConfigDlg, CDialogEx)

#define GX_VERIFY_EXIT(x) if(!x) {return FALSE;}

CGxDeviceConfigDlg::CGxDeviceConfigDlg(CGXFeatureControlPointer objStreamFeatureControlPtr, bool bIsSnap, CWnd* pParent /*=NULL*/)
	: CDialog(IDD_DEVICECONFIG, pParent)
	, m_bProgrammaticChange(false)
{
	m_objStreamFeatureControlPtr = objStreamFeatureControlPtr;
	m_bIsSnap = bIsSnap;
}

CGxDeviceConfigDlg::~CGxDeviceConfigDlg()
{
}

void CGxDeviceConfigDlg::DoDataExchange(CDataExchange* pDX)
{
	CDialog::DoDataExchange(pDX);
	DDX_Text(pDX, IDC_ENCODERDRVIDER, m_nEncoderDrvider);
	DDX_Text(pDX, IDC_ENCODERVALUE, m_nEncoderValue);
	DDX_Text(pDX, IDC_SPECTRUMROIOFFSETY, m_nSpectrumROIOffsetY);
	DDX_Text(pDX, IDC_SPECTRUMROIHEIGHT, m_nSpectrumROIHeight);
	DDX_Text(pDX, IDC_EXPOSURETIME, m_fExposureTime);
	DDX_Text(pDX, IDC_GAIN, m_fGain);
}

BOOL CGxDeviceConfigDlg::OnInitDialog()
{
	CDialog::OnInitDialog();

	try
	{
		m_bProgrammaticChange = true;
		GX_VERIFY_EXIT(__UpdateComBoBoxList("TriggerMode", IDC_TRIGGERMODE));
		GX_VERIFY_EXIT(__UpdateComBoBoxList("TriggerSource", IDC_TRIGGERSOURCE));
		GX_VERIFY_EXIT(__UpdateComBoBoxList("EncoderMode", IDC_ENCODERMODE));
		GX_VERIFY_EXIT(__UpdateComBoBoxList("EncoderOutputMode", IDC_ENCODEROUTPUTMODE));
		GX_VERIFY_EXIT(__UpdateComBoBoxList("SpectrumSelector", IDC_SPECTRUMSELECTOR));
		GX_VERIFY_EXIT(__UpdateIntValue("EncoderDivider", IDC_ENCODERDRVIDER, m_nEncoderDrvider));
		GX_VERIFY_EXIT(__UpdateIntValue("EncoderValue", IDC_ENCODERVALUE, m_nEncoderValue));
		GX_VERIFY_EXIT(__UpdateIntValue("SpectrumROIOffsetY", IDC_SPECTRUMROIOFFSETY, m_nSpectrumROIOffsetY));
		GX_VERIFY_EXIT(__UpdateIntValue("SpectrumROIHeight", IDC_SPECTRUMROIHEIGHT, m_nSpectrumROIHeight));
		GX_VERIFY_EXIT(__UpdateFloatValue("ExposureTime", IDC_EXPOSURETIME, m_fExposureTime));
		GX_VERIFY_EXIT(__UpdateFloatValue("Gain", IDC_GAIN, m_fGain));
		m_bProgrammaticChange = false;

		if (m_bIsSnap)
		{
			GetDlgItem(IDC_SPECTRUMROIOFFSETY)->EnableWindow(!m_bIsSnap);
			GetDlgItem(IDC_SPECTRUMROIHEIGHT)->EnableWindow(!m_bIsSnap);
		}
	}
	catch (CGalaxyException& e)
	{
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		MessageBox(e.what());
	}

	return TRUE;
}


BEGIN_MESSAGE_MAP(CGxDeviceConfigDlg, CDialog)
	ON_CBN_SELCHANGE(IDC_TRIGGERMODE, &CGxDeviceConfigDlg::OnSelchangeTriggermode)
	ON_CBN_SELCHANGE(IDC_TRIGGERSOURCE, &CGxDeviceConfigDlg::OnSelchangeTriggersource)
	ON_CBN_SELCHANGE(IDC_ENCODERMODE, &CGxDeviceConfigDlg::OnSelchangeEncodermode)
	ON_CBN_SELCHANGE(IDC_ENCODEROUTPUTMODE, &CGxDeviceConfigDlg::OnSelchangeEncoderoutputmode)
	ON_EN_KILLFOCUS(IDC_ENCODERVALUE, &CGxDeviceConfigDlg::OnChangeEncodervalue)
	ON_EN_KILLFOCUS(IDC_EXPOSURETIME, &CGxDeviceConfigDlg::OnChangeExposuretime)
	ON_EN_KILLFOCUS(IDC_GAIN, &CGxDeviceConfigDlg::OnChangeGain)
	ON_EN_KILLFOCUS(IDC_SPECTRUMROIHEIGHT, &CGxDeviceConfigDlg::OnChangeSpectrumroiheight)
	ON_EN_KILLFOCUS(IDC_SECPTRUMROIOFFSETY, &CGxDeviceConfigDlg::OnChangeSpectrumroioffsety)
	ON_CBN_SELCHANGE(IDC_SPECTRUMSELECTOR, &CGxDeviceConfigDlg::OnSelchangeSpectrumselector)
	ON_EN_KILLFOCUS(IDC_ENCODERDRVIDER, &CGxDeviceConfigDlg::OnChangeEncoderdrvider)
END_MESSAGE_MAP()


// CDeviceConfig 消息处理程序


BOOL CGxDeviceConfigDlg::PreTranslateMessage(MSG * pMsg)
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
		case IDC_ENCODERVALUE:
		case IDC_EXPOSURETIME:
		case IDC_GAIN:
		case IDC_SPECTRUMROIHEIGHT:
		case IDC_SECPTRUMROIOFFSETY:
		case IDC_ENCODERDRVIDER:

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

void CGxDeviceConfigDlg::OnSelchangeTriggermode()
{
	CComboBox* pCombo = NULL;
	try
	{
		pCombo = (CComboBox*)GetDlgItem(IDC_TRIGGERMODE);
		int nIndex = pCombo->GetCurSel();

		CString strValue;
		pCombo->GetLBText(nIndex, strValue);
		m_objStreamFeatureControlPtr->GetEnumFeature("TriggerMode")->SetValue(strValue.GetString());
		m_mapOldValue[IDC_TRIGGERMODE] = nIndex;
	}
	catch (CGalaxyException& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_TRIGGERMODE]);
		MessageBox(e.what());
		return;
	}
	catch (std::exception& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_TRIGGERMODE]);
		MessageBox(e.what());
		return;
	}
}


void CGxDeviceConfigDlg::OnSelchangeTriggersource()
{
	CComboBox* pCombo = NULL;
	try
	{
		pCombo = (CComboBox*)GetDlgItem(IDC_TRIGGERSOURCE);
		int nIndex = pCombo->GetCurSel();

		CString strValue;
		pCombo->GetLBText(nIndex, strValue);
		m_objStreamFeatureControlPtr->GetEnumFeature("TriggerSource")->SetValue(strValue.GetString());
		m_mapOldValue[IDC_TRIGGERSOURCE] = nIndex;
	}
	catch (CGalaxyException& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_TRIGGERSOURCE]);
		MessageBox(e.what());
		return;
	}
	catch (std::exception& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_TRIGGERSOURCE]);
		MessageBox(e.what());
		return;
	}
}


void CGxDeviceConfigDlg::OnSelchangeEncodermode()
{
	CComboBox* pCombo = NULL;
	try
	{
		pCombo = (CComboBox*)GetDlgItem(IDC_ENCODERMODE);
		int nIndex = pCombo->GetCurSel();

		CString strValue;
		pCombo->GetLBText(nIndex, strValue);
		m_objStreamFeatureControlPtr->GetEnumFeature("EncoderMode")->SetValue(strValue.GetString());
		m_mapOldValue[IDC_ENCODERMODE] = nIndex;

		m_bProgrammaticChange = true;
		__UpdateComBoBoxList("EncoderOutputMode", IDC_ENCODEROUTPUTMODE);
		m_bProgrammaticChange = false;
	}
	catch (CGalaxyException& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_ENCODERMODE]);
		MessageBox(e.what());
		return;
	}
	catch (std::exception& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_ENCODERMODE]);
		MessageBox(e.what());
		return;
	}
}


void CGxDeviceConfigDlg::OnSelchangeEncoderoutputmode()
{
	CComboBox* pCombo = NULL;
	try
	{
		pCombo = (CComboBox*)GetDlgItem(IDC_ENCODEROUTPUTMODE);
		int nIndex = pCombo->GetCurSel();

		CString strValue;
		pCombo->GetLBText(nIndex, strValue);
		m_objStreamFeatureControlPtr->GetEnumFeature("EncoderOutputMode")->SetValue(strValue.GetString());
		m_mapOldValue[IDC_ENCODEROUTPUTMODE] = nIndex;
	}
	catch (CGalaxyException& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_ENCODEROUTPUTMODE]);
		MessageBox(e.what());
		return;
	}
	catch (std::exception& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_ENCODEROUTPUTMODE]);
		MessageBox(e.what());
		return;
	}
}

void CGxDeviceConfigDlg::OnChangeEncoderdrvider()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	int nOldValue = m_nEncoderDrvider;
	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_nEncoderDrvider > m_mapIntRange[IDC_ENCODERDRVIDER]->GetMax())
		{
			m_nEncoderDrvider = m_mapIntRange[IDC_ENCODERDRVIDER]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_nEncoderDrvider < m_mapIntRange[IDC_ENCODERDRVIDER]->GetMin())
		{
			m_nEncoderDrvider = m_mapIntRange[IDC_ENCODERDRVIDER]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetIntFeature("EncoderDivider")->SetValue(m_nEncoderDrvider);
	}
	catch (CGalaxyException& e)
	{
		m_nEncoderDrvider = nOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_nEncoderDrvider = nOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}

void CGxDeviceConfigDlg::OnChangeEncodervalue()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	int nOldValue = m_nEncoderValue;
	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_nEncoderValue > m_mapIntRange[IDC_ENCODERVALUE]->GetMax())
		{
			m_nEncoderValue = m_mapIntRange[IDC_ENCODERVALUE]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_nEncoderValue < m_mapIntRange[IDC_ENCODERVALUE]->GetMin())
		{
			m_nEncoderValue = m_mapIntRange[IDC_ENCODERVALUE]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetIntFeature("EncoderValue")->SetValue(m_nEncoderValue);
	}
	catch (CGalaxyException& e)
	{
		m_nEncoderValue = nOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_nEncoderValue = nOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}

void CGxDeviceConfigDlg::OnSelchangeSpectrumselector()
{
	CComboBox* pCombo = NULL;
	try
	{
		pCombo = (CComboBox*)GetDlgItem(IDC_SPECTRUMSELECTOR);
		int nIndex = pCombo->GetCurSel();

		CString strValue;
		pCombo->GetLBText(nIndex, strValue);
		std::string dd = strValue.GetString();
		m_objStreamFeatureControlPtr->GetEnumFeature("SpectrumSelector")->SetValue(strValue.GetString());
		m_mapOldValue[IDC_SPECTRUMSELECTOR] = nIndex;

		m_bProgrammaticChange = true;
		__UpdateIntValue("SpectrumROIOffsetY", IDC_SPECTRUMROIOFFSETY, m_nSpectrumROIOffsetY);
		m_bProgrammaticChange = false;
	}
	catch (CGalaxyException& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_SPECTRUMSELECTOR]);
		MessageBox(e.what());
		return;
	}
	catch (std::exception& e)
	{
		pCombo->SetCurSel(m_mapOldValue[IDC_SPECTRUMSELECTOR]);
		MessageBox(e.what());
		return;
	}
}

void CGxDeviceConfigDlg::OnChangeExposuretime()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	float dOldValue = m_fExposureTime;

	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_fExposureTime > m_mapFloatRange[IDC_EXPOSURETIME]->GetMax())
		{
			m_fExposureTime = m_mapFloatRange[IDC_EXPOSURETIME]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_fExposureTime < m_mapFloatRange[IDC_EXPOSURETIME]->GetMin())
		{
			m_fExposureTime = m_mapFloatRange[IDC_EXPOSURETIME]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetFloatFeature("ExposureTime")->SetValue(m_fExposureTime);
	}
	catch (CGalaxyException& e)
	{
		m_fExposureTime = dOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_fExposureTime = dOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}


void CGxDeviceConfigDlg::OnChangeGain()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	float dOldValue = m_fGain;

	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_fGain > m_mapFloatRange[IDC_GAIN]->GetMax())
		{
			m_fGain = m_mapFloatRange[IDC_GAIN]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_fGain < m_mapFloatRange[IDC_GAIN]->GetMin())
		{
			m_fGain = m_mapFloatRange[IDC_GAIN]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetFloatFeature("Gain")->SetValue(m_fGain);
	}
	catch (CGalaxyException& e)
	{
		m_fGain = dOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_fGain = dOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}


void CGxDeviceConfigDlg::OnChangeSpectrumroiheight()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	int nOldValue = m_nSpectrumROIHeight;
	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_nSpectrumROIHeight > m_mapIntRange[IDC_SPECTRUMROIHEIGHT]->GetMax())
		{
			m_nSpectrumROIHeight = m_mapIntRange[IDC_SPECTRUMROIHEIGHT]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_nSpectrumROIHeight < m_mapIntRange[IDC_SPECTRUMROIHEIGHT]->GetMin())
		{
			m_nSpectrumROIHeight = m_mapIntRange[IDC_SPECTRUMROIHEIGHT]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetIntFeature("SpectrumROIHeight")->SetValue(m_nSpectrumROIHeight);
	}
	catch (CGalaxyException& e)
	{
		m_nSpectrumROIHeight = nOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_nSpectrumROIHeight = nOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}


void CGxDeviceConfigDlg::OnChangeSpectrumroioffsety()
{
	if (m_bProgrammaticChange)
	{
		return;
	}

	int nOldValue = m_nSpectrumROIOffsetY;
	try
	{
		UpdateData(TRUE);
		//若大于最大值则将设为最大值
		if (m_nSpectrumROIOffsetY > m_mapIntRange[IDC_SPECTRUMROIOFFSETY]->GetMax())
		{
			m_nSpectrumROIOffsetY = m_mapIntRange[IDC_SPECTRUMROIOFFSETY]->GetMax();
		}
		//若小于最小值将设为最小值
		if (m_nSpectrumROIOffsetY < m_mapIntRange[IDC_SPECTRUMROIOFFSETY]->GetMin())
		{
			m_nSpectrumROIOffsetY = m_mapIntRange[IDC_SPECTRUMROIOFFSETY]->GetMin();
		}

		m_objStreamFeatureControlPtr->GetIntFeature("SpectrumROIOffsetY")->SetValue(m_nSpectrumROIOffsetY);
	}
	catch (CGalaxyException& e)
	{
		m_nSpectrumROIOffsetY = nOldValue;
		MessageBox(e.what());
	}
	catch (std::exception& e)
	{
		m_nSpectrumROIOffsetY = nOldValue;
		MessageBox(e.what());
	}

	UpdateData(FALSE);
}

//---------------------------------------------------------------------------------
/**
\brief   更新Int值
\return  [in]strName	值名称
\return  [in]nID		值ID
\return  更新是否成功
*/
//----------------------------------------------------------------------------------
bool CGxDeviceConfigDlg::__UpdateComBoBoxList(const std::string& strName, const int& nID)
{
	m_mapOldValue[nID] = -1;
	GxIAPICPP::gxstring_vector vecEnum = m_objStreamFeatureControlPtr->GetEnumFeature(strName.c_str())->GetEnumEntryList();
	GxIAPICPP::gxstring curValue = m_objStreamFeatureControlPtr->GetEnumFeature(strName.c_str())->GetValue();

	CComboBox* pCombo = (CComboBox*)GetDlgItem(nID);
	if (NULL == pCombo)
	{
		return false;
	}
	pCombo->ResetContent();

	for (int i=0; i< vecEnum.size(); ++i)
	{
		pCombo->AddString(vecEnum[i].c_str());
	}

	for (int i = 0; i < vecEnum.size(); ++i)
	{
		if (vecEnum[i] == curValue)
		{
			m_mapOldValue[nID] = i;
			pCombo->SetCurSel(i);
		}
	}

	return true;
}

//---------------------------------------------------------------------------------
/**
\brief   更新Int类型控件值
\return  [in]strName	值名称
\return  [in]nID		值ID
\return  更新是否成功
*/
//----------------------------------------------------------------------------------
bool CGxDeviceConfigDlg::__UpdateIntValue(const std::string& strName, const int& nID, int& nValue)
{
	CIntFeaturePointer pIntFeature = m_objStreamFeatureControlPtr->GetIntFeature(strName.c_str());
	m_mapIntRange[nID] = pIntFeature;
	int64_t curValue = pIntFeature->GetValue();

	CEdit * pEdit = (CEdit *)GetDlgItem(nID);
	if (NULL == pEdit)
	{
		return false;
	}

	nValue = curValue;
	std::string strValue = std::to_string(curValue);
	pEdit->SetWindowText(strValue.c_str());
	return true;
}

//---------------------------------------------------------------------------------
/**
\brief   更新Float类型控件值
\return  [in]strName	值名称
\return  [in]nID		值ID
\return  更新是否成功
*/
//----------------------------------------------------------------------------------
bool CGxDeviceConfigDlg::__UpdateFloatValue(const std::string& strName, const int& nID, float& fValue)
{
	CFloatFeaturePointer pFloatFeature = m_objStreamFeatureControlPtr->GetFloatFeature(strName.c_str());
	m_mapFloatRange[nID] = pFloatFeature;
	double curValue = pFloatFeature->GetValue();
	CEdit * pEdit = (CEdit *)GetDlgItem(nID);
	if (NULL == pEdit)
	{
		return false;
	}

	fValue = curValue;
	std::ostringstream oss;
	oss << curValue;
	std::string strValue = oss.str();
	pEdit->SetWindowText(strValue.c_str());
	return true;
}
