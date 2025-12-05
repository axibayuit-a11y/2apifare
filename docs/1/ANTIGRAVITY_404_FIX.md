# Antigravity 404 错误修复指南

## 🔍 问题根源

从日志分析和代码检查，发现 Antigravity 模型（Gemini 3 Pro High/Low）出现 404 错误。

### 关键发现

#### 1. 镜像端点配置

**Antigravity 镜像代理（ant.txt）：**
```javascript
const routeMap = {
  '/daily': 'daily-cloudcode-pa.sandbox.googleapis.com',
  '/autopush': 'autopush-cloudcode-pa.sandbox.googleapis.com',
  '/oauth2': 'oauth2.googleapis.com'
};
```

#### 2. 当前代码的默认端点

**在 `front/control_panel.html:8580-8582`：**
```javascript
antigravityApiEndpoint: 'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse',
antigravityModelsEndpoint: 'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels',
antigravityOauthEndpoint: 'https://oauth2.googleapis.com/token'
```

### 🚨 **问题所在**

当前配置**直接访问 Google 的 sandbox 端点**，没有经过 Cloudflare Worker 镜像！

#### 错误流程：

```
用户请求 ANT/gemini-3-pro-high
    ↓
代码直接请求：
https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent
    ↓
❌ Google 检测到未授权的直接访问
    ↓
返回 404 "Requested entity was not found"
```

#### 正确流程应该是：

```
用户请求 ANT/gemini-3-pro-high
    ↓
代码请求：
https://your-proxy.workers.dev/daily/v1internal:streamGenerateContent
    ↓
Cloudflare Worker 转发到：
https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent
    ↓
✅ Google 接受请求（看起来像来自 Cloudflare）
    ↓
返回成功响应
```

---

## 🎯 修复方案

### 方案 1：通过控制面板配置（推荐）

#### 步骤 1：获取你的镜像端点 URL

假设你的 Antigravity Worker 部署在：
```
https://your-antigravity-proxy.workers.dev
```

#### 步骤 2：登录控制面板配置

访问控制面板：`https://your-server/`

进入 **配置管理** → **Antigravity 配置**

配置正确的端点：

```
Antigravity API Endpoint:
https://your-antigravity-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse

Antigravity Models Endpoint:
https://your-antigravity-proxy.workers.dev/daily/v1internal:fetchAvailableModels

Antigravity OAuth Endpoint:
https://your-antigravity-proxy.workers.dev/oauth2/token
```

#### 步骤 3：保存并重启服务

保存配置后，重启服务：
```bash
docker restart 2apifare
```

---

### 方案 2：通过环境变量配置

在服务器的 `.env` 文件或 `docker-compose.yml` 中添加：

```bash
# .env 文件
ANTIGRAVITY_API_ENDPOINT=https://your-antigravity-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse
ANTIGRAVITY_MODELS_ENDPOINT=https://your-antigravity-proxy.workers.dev/daily/v1internal:fetchAvailableModels
ANTIGRAVITY_OAUTH_ENDPOINT=https://your-antigravity-proxy.workers.dev/oauth2/token
```

或在 `docker-compose.yml` 中：

```yaml
services:
  2apifare:
    environment:
      - ANTIGRAVITY_API_ENDPOINT=https://your-antigravity-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse
      - ANTIGRAVITY_MODELS_ENDPOINT=https://your-antigravity-proxy.workers.dev/daily/v1internal:fetchAvailableModels
      - ANTIGRAVITY_OAUTH_ENDPOINT=https://your-antigravity-proxy.workers.dev/oauth2/token
```

重启服务：
```bash
docker-compose down
docker-compose up -d
```

---

### 方案 3：修改代码默认值（永久修复）

如果想让默认配置就是正确的，可以修改代码：

**文件：`front/control_panel.html`**

找到第 8580-8582 行（或搜索 `antigravityApiEndpoint`）：

```javascript
// 修改前：
antigravityApiEndpoint: 'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse',
antigravityModelsEndpoint: 'https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels',
antigravityOauthEndpoint: 'https://oauth2.googleapis.com/token'

// 修改后：
antigravityApiEndpoint: 'https://your-antigravity-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse',
antigravityModelsEndpoint: 'https://your-antigravity-proxy.workers.dev/daily/v1internal:fetchAvailableModels',
antigravityOauthEndpoint: 'https://your-antigravity-proxy.workers.dev/oauth2/token'
```

**注意**：将 `your-antigravity-proxy.workers.dev` 替换为你实际的 Worker 域名。

---

## 🧪 验证修复

### 测试 1：检查当前配置

```bash
# 查看当前 Antigravity 端点配置
curl http://your-server/config/get \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  | grep antigravity

# 预期输出应该包含你的镜像 Worker 域名，而不是直接的 googleapis.com
```

### 测试 2：测试 Antigravity 模型

```bash
# 请求 Antigravity 模型
curl -X POST http://your-server/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ANT/gemini-3-pro-low",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# ✅ 应该返回正常响应
# ❌ 不应该返回 404 错误
```

### 测试 3：查看日志

```bash
docker logs 2apifare --tail 50 | grep Antigravity

# ✅ 应该看到成功的日志：
# [INFO] [Attempt 1/5] Using Antigravity account: xxx@gmail.com
# [INFO] Successfully received response from Antigravity

# ❌ 不应该看到：
# [ERROR] Google API returned status 404
```

---

## 🔧 备用端点配置（可选）

如果你想启用 Autopush 作为备用端点：

```bash
# 环境变量
ANTIGRAVITY_API_ENDPOINT_BACKUP=https://your-antigravity-proxy.workers.dev/autopush/v1internal:streamGenerateContent?alt=sse
```

或在控制面板的 **高级配置** 中添加。

当主端点（/daily）失败时，系统会自动切换到备用端点（/autopush）。

---

## 📊 端点对比

| 端点类型 | 错误配置（直接访问） | 正确配置（通过镜像） |
|---------|-------------------|-------------------|
| **API 端点** | ❌ https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse | ✅ https://your-proxy.workers.dev/daily/v1internal:streamGenerateContent?alt=sse |
| **Models 端点** | ❌ https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:fetchAvailableModels | ✅ https://your-proxy.workers.dev/daily/v1internal:fetchAvailableModels |
| **OAuth 端点** | ❌ https://oauth2.googleapis.com/token | ✅ https://your-proxy.workers.dev/oauth2/token |

---

## ⚠️ 常见问题

### Q1: 为什么直接访问会 404？

**A**: `daily-cloudcode-pa.sandbox.googleapis.com` 是 Google 的内部 sandbox 环境：
- 需要特定的请求头和认证
- 需要来自特定的 IP 或代理
- 直接访问会被拒绝

通过 Cloudflare Worker 镜像：
- 清洗请求头
- 伪装请求来源
- Google 认为是合法请求

### Q2: 我怎么知道我的镜像 Worker 地址？

**A**: 查看你的 Cloudflare Workers 部署：

1. 登录 Cloudflare Dashboard
2. 进入 **Workers & Pages**
3. 找到你部署的 Antigravity Worker
4. 查看 **预览 URL** 或 **自定义域名**

例如：
- `https://antigravity-proxy.your-account.workers.dev`
- 或自定义域名：`https://antigravity.yourdomain.com`

### Q3: OAuth 端点也需要镜像吗？

**A**: 是的！虽然 `oauth2.googleapis.com` 是公开的，但通过镜像有以下好处：

1. 统一请求来源（都来自 Cloudflare）
2. 避免 Google 关联不同的请求
3. 提高成功率

### Q4: 修复后还是 404 怎么办？

**A**: 检查以下几点：

1. **镜像 Worker 是否正常工作**：
   ```bash
   curl https://your-proxy.workers.dev/daily/v1internal:fetchAvailableModels \
     -H "Authorization: Bearer YOUR_TOKEN"
   ```

2. **路由配置是否正确**：
   检查 Worker 代码中的 `routeMap`

3. **访问 token 是否有效**：
   ```bash
   # 刷新 Antigravity token
   python refresh_antigravity_token.py
   ```

4. **账号权限是否足够**：
   某些 Antigravity 模型需要特殊权限

---

## 🎯 预期修复效果

### 修复前

```
[INFO] Using Antigravity model: gemini-3-pro-high
[ERROR] Google API returned status 404 (NON-STREAMING). Response details: {
  "error": {
    "code": 404,
    "message": "Requested entity was not found.",
    "status": "NOT_FOUND"
  }
}
```

### 修复后

```
[INFO] Using Antigravity model: gemini-3-pro-high
[INFO] [Antigravity] 使用主端点: https://your-proxy.workers.dev/daily/v1internal...
[INFO] Successfully received response from Antigravity
[INFO] Account 107368959964244939488 state - disabled: False
```

---

## 📝 检查清单

修复 Antigravity 404 错误的步骤：

- [ ] 确认镜像 Worker 已部署并可访问
- [ ] 获取镜像 Worker 的完整 URL
- [ ] 配置正确的 Antigravity 端点（通过控制面板或环境变量）
- [ ] 重启服务
- [ ] 测试 Antigravity 模型请求
- [ ] 查看日志确认成功
- [ ] （可选）配置备用端点

---

## 🔗 相关文档

- [GOOGLE_API_ENDPOINTS.md](./GOOGLE_API_ENDPOINTS.md) - API 端点参考
- [ANT前缀路由实现说明.md](./1/ANT前缀路由实现说明.md) - Antigravity 架构
- [SERVER_ISSUES_ANALYSIS_20251205.md](./SERVER_ISSUES_ANALYSIS_20251205.md) - 服务器问题分析
- Cloudflare Worker 代码：`docs/ant.txt`

---

## 💡 总结

**Antigravity 404 错误的根本原因**：
- ❌ 直接访问 Google sandbox 端点被拒绝
- ✅ 应该通过 Cloudflare Worker 镜像访问

**修复方法**：
1. 获取你的镜像 Worker URL
2. 配置正确的 Antigravity 端点
3. 重启服务
4. 验证修复

**预期效果**：
- Gemini 3 Pro High/Low 等模型可以正常使用
- 不再出现 404 错误
- 请求成功率提升
