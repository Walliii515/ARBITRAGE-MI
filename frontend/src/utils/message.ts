import { ElMessage } from 'element-plus'

const placement = 'top-right' as const

export function showSuccess(message: string) {
  ElMessage.success({ message, placement, duration: 3000 })
}

export function showWarning(message: string) {
  ElMessage.warning({ message, placement, duration: 3000 })
}

export function showError(message: string) {
  ElMessage.error({ message, placement, duration: 4000 })
}
