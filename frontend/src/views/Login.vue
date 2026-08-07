<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { setToken } from '../utils/auth'
import { ElMessage } from 'element-plus'

const router = useRouter()
const route = useRoute()
const loginForm = ref({
  username: '',
  password: ''
})
const loading = ref(false)

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

async function handleLogin() {
  if (!loginForm.value.username || !loginForm.value.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  
  loading.value = true
  try {
    const response = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(loginForm.value)
    })
    
    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || '登录失败')
    }
    
    const data = await response.json()
    setToken(data.token)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    const destination = redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
    router.replace(destination)
  } catch (error: any) {
    ElMessage.error(error.message || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-container">
    <div class="login-card">
      <h1 class="login-title">AI Powered Arbitrage</h1>
      <el-form :model="loginForm" @submit.prevent="handleLogin">
        <el-form-item>
          <el-input
            v-model="loginForm.username"
            placeholder="用户名"
            :prefix-icon="User"
            size="large"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="密码"
            :prefix-icon="Lock"
            size="large"
            show-password
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            size="large"
            :loading="loading"
            class="login-button"
            native-type="submit"
          >
            登录
          </el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-container {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #0d1117;
}

.login-card {
  width: 400px;
  padding: 40px;
  background-color: #161b22;
  border-radius: 8px;
  border: 1px solid #30363d;
}

.login-title {
  text-align: center;
  color: #c9d1d9;
  margin-bottom: 32px;
  font-size: 24px;
  font-weight: 600;
}

.login-button {
  width: 100%;
}
</style>
