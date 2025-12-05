# Google API 端点参考

> 用于配置代理和多端点降级

## 📋 端点总览

| 服务 | 主端点 | 备用端点 | 用途 |
|------|--------|----------|------|
| CLI (Code Assist) | cloudcode-pa.googleapis.com | - | GeminiCLI 反代 |
| Antigravity | daily-cloudcode-pa.sandbox.googleapis.com | autopush-cloudcode-pa.sandbox.googleapis.com | Antigravity 反代 |
| OAuth | oauth2.googleapis.com | - | Token 刷新 |
| Cloud Resource Manager | cloudresourcemanager.googleapis.com | - | 项目管理 |
| Service Usage | serviceusage.googleapis.com | - | 服务使用 |
| User Info | www.googleapis.com | - | 用户信息 |

---

## 🔧 CLI (GeminiCLI) 端点

### 主端点
```
https://cloudcode-pa.googleapis.com
```

### API 路径
```
POST /v1internal:generateContent          # 非流式生成
POST /v1internal:streamGenerateContent    # 流式生成
POST /v1internal:loadCodeAssist           # 加载 Code Assist
POST /v1internal:onboardUser              # 用户注册
```

### 完整 URL 示例
```
# 流式生成
https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse

# 非流式生成
https://cloudcode-pa.googleapis.com/v1internal:generateContent
```

### 备用端点
CLI 目前没有已知的备用端点，只有一个生产环境端点。

---

## 🚀 Antigravity 端点

### 主端点 (Daily)
```
https://daily-cloudcode-pa.sandbox.googleapis.com
```

### 备用端点 (Autopush)
```
https://autopush-cloudcode-pa.sandbox.googleapis.com
```

### API 路径
```
POST /v1internal:streamGenerateContent    # 流式生成
POST /v1internal:generateContent          # 非流式生成
POST /v1internal:fetchAvailableModels     # 获取可用模型
```

### 完整 URL 示例
```
# 主端点 - 流式生成
https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse

# 主端点 - 获取模型
https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels

# 备用端点 - 流式生成
https://autopush-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse

# 备用端点 - 获取模型
https://autopush-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels
```

### 端点说明
| 端点 | 环境 | 稳定性 | 说明 |
|------|------|--------|------|
| daily | 每日构建 | ⭐⭐⭐ 较稳定 | 推荐作为主端点 |
| autopush | 自动推送 | ⭐⭐ 可能不稳定 | 可作为备用 |

---

## 🔐 OAuth 端点

### Token 刷新
```
https://oauth2.googleapis.com/token
```

### 请求示例
```
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

client_id=xxx&client_secret=xxx&grant_type=refresh_token&refresh_token=xxx
```

---

## 📁 其他 Google API 端点

### Cloud Resource Manager (项目管理)
```
https://cloudresourcemanager.googleapis.com
```

### Service Usage (服务使用)
```
https://serviceusage.googleapis.com
```

### User Info (用户信息)
```
https://www.googleapis.com/oauth2/v1/userinfo
```

---

## 🌐 代理配置示例

### Cloudflare Worker 路由映射
```javascript
const routeMap = {
    '/oauth2': 'oauth2.googleapis.com',
    '/crm': 'cloudresourcemanager.googleapis.com',
    '/usage': 'serviceusage.googleapis.com',
    '/api': 'www.googleapis.com',
    '/code': 'cloudcode-pa.googleapis.com',
    
    // Antigravity 端点
    '/daily': 'daily-cloudcode-pa.sandbox.googleapis.com',
    '/autopush': 'autopush-cloudcode-pa.sandbox.googleapis.com',
};
```

### config.toml 配置示例
```toml
# CLI 端点（通过代理）
code_assist_endpoint = "https://your-proxy.workers.dev/code"

# Antigravity 主端点（通过代理）
antigravity_api_endpoint = "https://your-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse"
antigravity_models_endpoint = "https://your-proxy.workers.dev/daily/v1internal:fetchAvailableModels"

# Antigravity 备用端点（通过代理）- 未来支持
# antigravity_api_endpoint_backup = "https://your-proxy.workers.dev/autopush/v1internal:streamGenerateContent?alt=sse"
```

---

## ⚠️ 注意事项

1. **sandbox 端点** (`*.sandbox.googleapis.com`) 是 Google 内部测试环境，可能随时变化
2. **daily 端点** 相对稳定，推荐作为 Antigravity 主端点
3. **autopush 端点** 可能包含未发布的功能，稳定性较差
4. **CLI 端点** (`cloudcode-pa.googleapis.com`) 是生产环境，最稳定
5. 所有端点都需要有效的 OAuth Token 才能访问

---

*文档创建时间：2025-12-04*
