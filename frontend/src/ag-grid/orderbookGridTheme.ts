import { themeQuartz, colorSchemeDarkBlue } from 'ag-grid-community'

/** 贴近交易终端的深色紧凑表格主题 */
export const orderbookGridTheme = themeQuartz
  .withPart(colorSchemeDarkBlue)
  .withParams({
    fontFamily: [
      '-apple-system',
      'BlinkMacSystemFont',
      '"Segoe UI"',
      'Roboto',
      '"Helvetica Neue"',
      'Arial',
      'sans-serif',
    ],
    fontSize: 12,
    dataFontSize: 12,
    headerFontSize: 12,
    headerFontWeight: 600,
    spacing: 4,
    rowHeight: 32,
    headerHeight: 32,
    backgroundColor: '#181d1f',
    foregroundColor: '#e8eaed',
    chromeBackgroundColor: '#222628',
    borderColor: '#333333',
    headerColumnResizeHandleColor: '#555',
    selectedRowBackgroundColor: '#1b3a57',
    rowHoverColor: '#252a2d',
    accentColor: '#2196f3',
    /** 数值变更时单元格闪烁高亮背景 */
    valueChangeValueHighlightBackgroundColor: 'rgba(33, 150, 243, 0.45)',
    columnBorder: false,
    wrapperBorder: false,
  })
