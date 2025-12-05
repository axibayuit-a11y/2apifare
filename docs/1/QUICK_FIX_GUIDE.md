# 快速修复指南

## 🚨 立即部署修复（解决 429 延迟和 404 空响应）

### 步骤 1：提交并推送代码

```bash
cd d:\Research\fandai\2apifare

# 1. 查看修改内容
git status

# 2. 提交所有修改
git add .
git commit -m "fix: 修复重试逻辑的严重问题

- 修复 429 切换凭证后不必要的延迟（从 62 秒降至 <1 秒）
- 修复 404 错误空响应问题（返回明确错误而不是空内容）
- 修复 400 错误误刷新 token（参数错误应直接返回）
- 修复流式请求资源泄漏（防止连接池耗尽）
- 修复 thinking_budget 超出范围（512-24576）

详见：
- docs/RETRY_LOGIC_ANALYSIS.md
- docs/RETRY_LOGIC_FIXES_20251205.md
- docs/FIX_EMPTY_RESPONSE_20251205.md
- docs/SERVER_ISSUES_ANALYSIS_20251205.md
"

# 3. 推送到 GitHub
git push origin master
```

### 步骤 2：更新服务器

```bash
# SSH 到服务器
ssh user@your-server

# 进入项目目录
cd /path/to/2apifare

# 拉取最新代码
git pull origin master

# 重启服务
docker-compose down
docker-compose up -d

# 或如果不用 Docker
pkill -f "python.*web.py"
nohup python web.py > /dev/null 2>&1 &
```

### 步骤 3：验证修复

#### 验证 1：429 延迟修复

```bash
# 发送多个请求触发 429
for i in {1..20}; do
  curl -X POST http://your-server/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gemini-2.5-pro","messages":[{"role":"user","content":"test"}]}'
done

# 查看日志
docker logs 2apifare --tail 50 | grep "RETRY"

# ✅ 应该看到：
# [RETRY] 429 error, switched to new credential, retrying immediately
# ❌ 不应该看到：
# [RETRY] 429 error encountered, waiting 2.0s before retry
```

#### 验证 2：404 错误处理

```bash
# 触发 404 错误
curl -X POST http://your-server/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-pro-high","messages":[{"role":"user","content":"test"}]}'

# ✅ 应该看到明确的错误信息：
# {"error":{"message":"API error: 404","type":"api_error","code":404}}

# ❌ 不应该是空响应
```

#### 验证 3：400 错误直接返回

```bash
# 使用错误参数
curl -X POST http://your-server/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-pro","messages":[]}'

# ✅ 应该立即返回 400 错误
# ❌ 不应该有重试日志
```

---

## 🔧 额外修复（可选）

### 修复 1：移除 403 错误延迟

如果觉得有必要，可以进一步优化 403 错误的处理：

```python
# src/google_chat_api.py:454 (找到这一行)
await asyncio.sleep(0.5)  # 短暂延迟后重试

# 改为（移除延迟）：
# await asyncio.sleep(0.5)  # [REMOVED] 403 是永久错误，立即切换
```

### 修复 2：优化镜像端点

更新 Cloudflare Worker 代码（如果需要）：

```javascript
// 按路径长度排序，避免路由冲突
const routeMap = {
  '/oauth2': 'oauth2.googleapis.com',
  '/crm': 'cloudresourcemanager.googleapis.com',
  '/usage': 'serviceusage.googleapis.com',
  '/api': 'www.googleapis.com',
  '/code': 'cloudcode-pa.googleapis.com'
};

const sortedRoutes = Object.entries(routeMap)
  .sort((a, b) => b[0].length - a[0].length);

for (const [prefix, host] of sortedRoutes) {
  if (path.startsWith(prefix)) {
    targetHost = host;
    url.pathname = path.substring(prefix.length);  // 使用 substring
    break;
  }
}
```

---

## 📊 预期改进

### 部署前（当前服务器）

```
429 错误处理：
  切换凭证 5 次 → 等待 2+4+8+16+32 = 62 秒
  ❌ 用户体验极差

404 错误处理：
  返回空响应（200 -）
  ❌ 用户困惑，不知道发生了什么

400 错误处理：
  刷新 token → 切换凭证 → 重试 5 次 → 浪费 ~10 秒
  ❌ 参数错误无法通过重试解决
```

### 部署后（修复版本）

```
429 错误处理：
  切换凭证 → 立即重试 → 成功
  ✅ 响应时间 <1 秒（提升 60+ 倍）

404 错误处理：
  刷新 token → 成功 OR 切换凭证 → 成功
  失败后返回明确错误
  ✅ 用户能看到清晰的错误信息

400 错误处理：
  立即返回错误
  ✅ 用户能立即修复参数问题
```

---

## ⚠️ 注意事项

### 1. 备份数据

部署前确保备份：
- `creds/creds.toml`（凭证数据）
- `creds/accounts.toml`（Antigravity 账号）
- `creds/ip_data.json`（IP 统计）
- `config.toml`（配置文件）

```bash
# 备份命令
cd /path/to/2apifare
tar -czf backup-$(date +%Y%m%d-%H%M%S).tar.gz creds/ config.toml
```

### 2. 监控日志

部署后持续监控 30 分钟：

```bash
# 实时查看日志
docker logs -f 2apifare

# 或
tail -f /var/log/2apifare.log
```

### 3. 回滚方案

如果出现问题，快速回滚：

```bash
# 回滚到上一个版本
git reset --hard HEAD~1
git push -f origin master

# 重启服务
docker-compose restart
```

---

## 🎯 预期日志变化

### 部署前（旧日志）

```
[ERROR] Google API returned status 429
[WARNING] [RETRY] 429 error encountered, waiting 2.0s before retry (1/5)
[INFO] Rotated to credential index 4

[ERROR] Google API returned status 429
[WARNING] [RETRY] 429 error encountered, waiting 4.0s before retry (2/5)
[INFO] Rotated to credential index 5

... 继续等待 8s, 16s, 32s ...
```

### 部署后（新日志）

```
[ERROR] Google API returned status 429
[INFO] [RETRY] 429 error, switched to new credential, retrying immediately (1/5)
[INFO] Rotated to credential index 4

[INFO] Request succeeded with new credential  ← 成功！
```

---

## ✅ 成功标志

部署成功后，你应该看到：

1. ✅ 日志中没有 "waiting Xs before retry" 的 429 错误
2. ✅ 404 错误返回明确的错误信息，不是空响应
3. ✅ 400 错误立即返回，日志显示 "[BAD REQUEST]"
4. ✅ 用户反馈响应速度明显提升
5. ✅ 没有新的错误或崩溃

---

## 🔍 故障排查

### 问题 1：Git push 失败

```bash
# 如果遇到冲突
git pull --rebase origin master
git push origin master
```

### 问题 2：Docker 重启失败

```bash
# 查看容器日志
docker logs 2apifare

# 检查端口占用
netstat -tulpn | grep 8000

# 强制重建
docker-compose down
docker-compose up -d --force-recreate
```

### 问题 3：修复后仍有问题

```bash
# 1. 确认代码已更新
git log -1

# 2. 确认服务已重启
docker ps | grep 2apifare

# 3. 清理 Python 缓存
find . -type d -name __pycache__ -exec rm -r {} +
find . -type f -name "*.pyc" -delete

# 4. 重启服务
docker-compose restart
```

---

## 📞 需要帮助？

如果遇到问题：

1. 检查日志文件
2. 回滚到上一个版本
3. 查看文档：
   - [RETRY_LOGIC_ANALYSIS.md](./RETRY_LOGIC_ANALYSIS.md)
   - [SERVER_ISSUES_ANALYSIS_20251205.md](./SERVER_ISSUES_ANALYSIS_20251205.md)
