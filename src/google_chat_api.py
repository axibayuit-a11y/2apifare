"""
Google API Client - Handles all communication with Google's Gemini API.
This module is used by both OpenAI compatibility layer and native Gemini endpoints.
"""

import asyncio
import gc
import json
import re
import time

from fastapi import Response
from fastapi.responses import StreamingResponse

from config import (
    get_code_assist_endpoint,
    DEFAULT_SAFETY_SETTINGS,
    get_base_model_name,
    get_thinking_budget,
    should_include_thoughts,
    is_search_model,
    get_auto_ban_enabled,
    get_auto_ban_error_codes,
    get_retry_429_max_retries,
    get_retry_429_enabled,
    get_retry_429_interval,
    PUBLIC_API_MODELS,
)
from .httpx_client import http_client, create_streaming_client_with_kwargs
from log import log
from .credential_manager import CredentialManager
from .utils import get_user_agent


def _parse_quota_reset_delay(response_content: str) -> float:
    """
    从 429 响应中解析 quotaResetDelay，返回冷却截止时间戳
    
    Args:
        response_content: 429 响应内容
        
    Returns:
        冷却截止时间戳（Unix时间戳），如果解析失败返回 None
    """
    try:
        # 尝试解析 JSON
        data = json.loads(response_content)
        details = data.get("error", {}).get("details", [])
        
        for detail in details:
            metadata = detail.get("metadata", {})
            
            # 优先使用 quotaResetTimeStamp
            if "quotaResetTimeStamp" in metadata:
                from datetime import datetime, timezone
                timestamp_str = metadata["quotaResetTimeStamp"]
                # 解析 ISO 格式时间
                if timestamp_str.endswith("Z"):
                    timestamp_str = timestamp_str.replace("Z", "+00:00")
                reset_dt = datetime.fromisoformat(timestamp_str)
                return reset_dt.timestamp()
            
            # 回退到 quotaResetDelay
            if "quotaResetDelay" in metadata:
                delay_str = metadata["quotaResetDelay"]
                # 解析格式如 "12h11m35.9550198s"
                total_seconds = 0
                
                # 匹配小时
                hours_match = re.search(r'(\d+)h', delay_str)
                if hours_match:
                    total_seconds += int(hours_match.group(1)) * 3600
                
                # 匹配分钟
                minutes_match = re.search(r'(\d+)m', delay_str)
                if minutes_match:
                    total_seconds += int(minutes_match.group(1)) * 60
                
                # 匹配秒（可能有小数）
                seconds_match = re.search(r'([\d.]+)s', delay_str)
                if seconds_match:
                    total_seconds += float(seconds_match.group(1))
                
                if total_seconds > 0:
                    return time.time() + total_seconds
        
        return None
    except Exception as e:
        log.debug(f"Failed to parse quota reset delay: {e}")
        return None


def _create_error_response(message: str, status_code: int = 500) -> Response:
    """Create standardized error response."""
    return Response(
        content=json.dumps(
            {"error": {"message": message, "type": "api_error", "code": status_code}}
        ),
        status_code=status_code,
        media_type="application/json",
    )


def _merge_safety_settings(user_settings: list = None) -> list:
    """
    合并用户的安全设置和默认安全设置。
    采用增量补充策略：只添加用户未配置的默认设置项，避免覆盖用户自定义设置。

    Args:
        user_settings: 用户提供的安全设置列表，可以为 None

    Returns:
        合并后的安全设置列表
    """
    if user_settings is None:
        # 用户未提供任何安全设置，返回全部默认设置
        return DEFAULT_SAFETY_SETTINGS.copy()

    # 提取用户已配置的 category 集合
    user_categories = {setting.get("category") for setting in user_settings if "category" in setting}

    # 创建合并后的设置列表（从用户设置开始）
    merged_settings = user_settings.copy()

    # 只追加用户未配置的默认设置项
    for default_setting in DEFAULT_SAFETY_SETTINGS:
        if default_setting.get("category") not in user_categories:
            merged_settings.append(default_setting)

    return merged_settings


async def _handle_api_error(
    credential_manager: CredentialManager,
    status_code: int,
    response_content: str = "",
    current_credential_file: str = None
):
    """Handle API errors by rotating credentials when needed. Error recording should be done before calling this function."""
    # 处理自动封禁的错误码（401, 403, 404, 429 等错误）
    if (
        await get_auto_ban_enabled()
        and status_code in await get_auto_ban_error_codes()
        and credential_manager
    ):
        if response_content:
            log.error(
                f"Google API returned status {status_code} - auto ban triggered. Response details: {response_content[:500]}"
            )
        else:
            log.warning(
                f"Google API returned status {status_code} - auto ban triggered"
            )

        # 禁用出错的凭证（403等是永久性错误）
        if current_credential_file:
            log.warning(f"Disabling credential due to {status_code} error: {current_credential_file}")
            await credential_manager.set_cred_disabled(current_credential_file, True)

        # 切换到下一个凭证
        await credential_manager.force_rotate_credential()


# ============ 辅助函数（消除重复代码，提高可维护性） ============


async def _check_should_auto_ban(status_code: int) -> bool:
    """检查是否应该触发自动封禁

    Args:
        status_code: HTTP 状态码

    Returns:
        bool: True 表示应该触发自动封禁，False 表示不需要
    """
    return (
        await get_auto_ban_enabled()
        and status_code in await get_auto_ban_error_codes()
    )


async def _handle_auto_ban(
    credential_manager: CredentialManager,
    status_code: int,
    credential_name: str
) -> None:
    """处理自动封禁：禁用凭证并轮换

    Args:
        credential_manager: 凭证管理器实例
        status_code: HTTP 状态码
        credential_name: 凭证文件名
    """
    if credential_manager and credential_name:
        log.warning(
            f"[AUTO_BAN] Status {status_code} triggers auto-ban, "
            f"disabling credential: {credential_name}"
        )
        await credential_manager.set_cred_disabled(credential_name, True)
        await credential_manager.force_rotate_credential()


async def _get_next_credential(
    credential_manager: CredentialManager,
    payload: dict,
    use_public_api: bool,
    target_url: str
):
    """获取下一个可用凭证并准备请求参数

    Args:
        credential_manager: 凭证管理器实例
        payload: 请求 payload
        use_public_api: 是否使用公共 API
        target_url: 目标 URL

    Returns:
        tuple: (current_file, credential_data, headers, final_post_data, target_url)
        None: 没有可用凭证
    """
    new_credential_result = await credential_manager.get_valid_credential()
    if new_credential_result:
        current_file, credential_data = new_credential_result
        headers, updated_payload, target_url = (
            await _prepare_request_headers_and_payload(
                payload, credential_data, use_public_api, target_url
            )
        )
        final_post_data = json.dumps(updated_payload)
        return current_file, credential_data, headers, final_post_data, target_url
    return None


async def _prepare_request_headers_and_payload(
    payload: dict, credential_data: dict, use_public_api: bool, target_url: str
):
    """Prepare request headers and final payload from credential data."""
    token = credential_data.get("token") or credential_data.get("access_token", "")
    if not token:
        raise Exception("凭证中没有找到有效的访问令牌（token或access_token字段）")

    source_request = payload.get("request", {})
    if use_public_api:
        if "generationConfig" in source_request:
            imageConfig = source_request["generationConfig"].get("imageConfig")
            source_request["generationConfig"] = {"imageConfig": imageConfig} if imageConfig else {}

    # 内部API使用Bearer Token和项目ID
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": get_user_agent(),
    }
    project_id = credential_data.get("project_id", "")
    if not project_id:
        raise Exception("项目ID不存在于凭证数据中")
    final_payload = {
        "model": payload.get("model"),
        "project": project_id,
        "request": source_request,
    }

    return headers, final_payload, target_url


async def send_gemini_request(
    payload: dict, is_streaming: bool = False, credential_manager: CredentialManager = None
) -> Response:
    """
    Send a request to Google's Gemini API.

    Args:
        payload: The request payload in Gemini format
        is_streaming: Whether this is a streaming request
        credential_manager: CredentialManager instance

    Returns:
        FastAPI Response object
    """
    # 获取429重试配置
    max_retries = await get_retry_429_max_retries()
    retry_429_enabled = await get_retry_429_enabled()
    retry_interval = await get_retry_429_interval()

    # 动态确定API端点和payload格式
    model_name = payload.get("model", "")
    base_model_name = get_base_model_name(model_name)
    use_public_api = base_model_name in PUBLIC_API_MODELS
    action = "streamGenerateContent" if is_streaming else "generateContent"
    target_url = f"{await get_code_assist_endpoint()}/v1internal:{action}"
    if is_streaming:
        target_url += "?alt=sse"

    # 确保有credential_manager
    if not credential_manager:
        return _create_error_response("Credential manager not provided", 500)

    # 获取当前凭证
    try:
        credential_result = await credential_manager.get_valid_credential()
        if not credential_result:
            return _create_error_response("No valid credentials available", 500)

        current_file, credential_data = credential_result
        headers, final_payload, target_url = await _prepare_request_headers_and_payload(
            payload, credential_data, use_public_api, target_url
        )
    except Exception as e:
        return _create_error_response(str(e), 500)

    # 预序列化payload，避免重试时重复序列化
    final_post_data = json.dumps(final_payload)

    for attempt in range(max_retries + 1):
        try:
            if is_streaming:
                # 流式请求处理 - 使用httpx_client模块的统一配置
                client = await create_streaming_client_with_kwargs()

                try:
                    # 使用stream方法但不在async with块中消费数据
                    stream_ctx = client.stream(
                        "POST", target_url, content=final_post_data, headers=headers
                    )
                    resp = await stream_ctx.__aenter__()

                    if resp.status_code == 429:
                        # 记录429错误并获取响应内容
                        response_content = ""
                        try:
                            content_bytes = await resp.aread()
                            if isinstance(content_bytes, bytes):
                                response_content = content_bytes.decode("utf-8", errors="ignore")
                        except Exception as e:
                            log.debug(f"[STREAMING] Failed to read 429 response content: {e}")

                        # 显示详细的429错误信息
                        if response_content:
                            log.error(
                                f"Google API returned status 429 (STREAMING). Response details: {response_content[:500]}"
                            )
                        else:
                            log.error(
                                "Google API returned status 429 (STREAMING) - quota exhausted, no response details available"
                            )

                        # 解析冷却时间并记录
                        cooldown_until = _parse_quota_reset_delay(response_content) if response_content else None
                        if credential_manager and current_file:
                            await credential_manager.record_api_call_result(
                                current_file, False, 429, cooldown_until=cooldown_until
                            )

                        # 清理资源
                        try:
                            await stream_ctx.__aexit__(None, None, None)
                        except:
                            pass
                        await client.aclose()

                        # 如果重试可用且未达到最大次数，进行重试
                        if retry_429_enabled and attempt < max_retries:
                            if credential_manager:
                                # 429错误时强制轮换凭证，不增加调用计数
                                await credential_manager.force_rotate_credential()
                                # 获取下一个凭证
                                next_cred_result = await _get_next_credential(
                                    credential_manager, payload, use_public_api, target_url
                                )
                                if next_cred_result:
                                    current_file, credential_data, headers, final_post_data, target_url = next_cred_result
                                    # [FIX] 成功切换到新凭证，立即重试（新凭证可能有配额）
                                    log.info(f"[RETRY] 429 error, switched to new credential, retrying immediately ({attempt + 1}/{max_retries})")
                                    continue
                                else:
                                    # 没有其他可用凭证，指数退避
                                    delay = retry_interval * (2 ** attempt)
                                    log.warning(
                                        f"[RETRY] 429 error, no other credentials available, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                                    )
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                # 没有凭证管理器，指数退避
                                delay = retry_interval * (2 ** attempt)
                                log.warning(
                                    f"[RETRY] 429 error encountered, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                                )
                                await asyncio.sleep(delay)
                                continue
                        else:
                            # [FIX] 返回普通 Response 而不是 StreamingResponse
                            # 避免抗截断机制误判错误响应为需要续写的截断内容
                            return _create_error_response(
                                "429 rate limit exceeded, max retries reached", 429
                            )
                    elif resp.status_code != 200:
                        # 处理其他非200状态码的错误
                        response_content = ""
                        try:
                            content_bytes = await resp.aread()
                            if isinstance(content_bytes, bytes):
                                response_content = content_bytes.decode("utf-8", errors="ignore")
                        except Exception as e:
                            log.debug(f"[STREAMING] Failed to read error response content: {e}")

                        # 显示详细的错误信息
                        if response_content:
                            log.error(
                                f"Google API returned status {resp.status_code} (STREAMING). Response details: {response_content[:500]}"
                            )
                        else:
                            log.error(
                                f"Google API returned status {resp.status_code} (STREAMING) - no response details available"
                            )

                        # 记录API调用错误
                        if credential_manager and current_file:
                            await credential_manager.record_api_call_result(
                                current_file, False, resp.status_code
                            )

                        # 清理资源
                        try:
                            await stream_ctx.__aexit__(None, None, None)
                        except:
                            pass
                        await client.aclose()

                        # 5xx 服务器错误：不切换凭证，直接等待重试
                        if 500 <= resp.status_code < 600 and attempt < max_retries:
                            # 指数退避：base_delay * 2^attempt (1s, 2s, 4s, 8s...)
                            delay = retry_interval * (2 ** attempt)
                            log.warning(
                                f"[RETRY] Server error {resp.status_code}, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                            )
                            await asyncio.sleep(delay)
                            continue  # 使用同一凭证重试

                        # [FIX] 400 错误是参数错误，直接返回，不刷新 token
                        if resp.status_code == 400:
                            log.error(
                                f"[BAD REQUEST] Invalid request parameters. Response: {response_content[:500]}"
                            )
                            # 清理资源
                            try:
                                await stream_ctx.__aexit__(None, None, None)
                            except:
                                pass
                            try:
                                await client.aclose()
                            except:
                                pass

                            return _create_error_response(
                                f"Invalid request: {response_content}", 400
                            )

                        # 检查是否是自动封禁错误码（403, 401, 404）且可以重试
                        should_auto_ban = await _check_should_auto_ban(resp.status_code)

                        if should_auto_ban and credential_manager and attempt < max_retries:
                            # 401/404 错误：先尝试刷新 token（可能是 token 过期）
                            if resp.status_code in (401, 404):
                                log.warning(
                                    f"[AUTH REFRESH] {resp.status_code} error, attempting token refresh before retry ({attempt + 1}/{max_retries})"
                                )
                                refresh_success = await credential_manager.force_refresh_current_token()

                                if refresh_success:
                                    # Token 刷新成功，使用同一凭证重试
                                    log.info("[AUTH REFRESH] Token refreshed, retrying with same credential")
                                    # 清理当前资源
                                    try:
                                        await stream_ctx.__aexit__(None, None, None)
                                    except:
                                        pass
                                    try:
                                        await client.aclose()
                                    except:
                                        pass

                                    # 重新准备请求头（使用刷新后的 token）
                                    next_cred_result = await _get_next_credential(
                                        credential_manager, payload, use_public_api, target_url
                                    )
                                    if next_cred_result:
                                        current_file, credential_data, headers, final_post_data, target_url = next_cred_result
                                    await asyncio.sleep(0.5)
                                    continue  # 重试

                                # Token 刷新失败，继续走封禁流程
                                log.warning("[AUTH REFRESH] Token refresh failed, proceeding with credential ban")

                            # 403 或 token 刷新失败：封禁当前凭证并切换到下一个凭证重试
                            log.warning(
                                f"[RETRY] {resp.status_code} error encountered, rotating credential and retrying ({attempt + 1}/{max_retries})"
                            )
                            # 禁用当前凭证并轮换
                            await _handle_auto_ban(credential_manager, resp.status_code, current_file)

                            # [FIX] 清理当前资源（防止资源泄漏）
                            try:
                                await stream_ctx.__aexit__(None, None, None)
                            except:
                                pass
                            try:
                                await client.aclose()
                            except:
                                pass

                            # 获取下一个凭证
                            next_cred_result = await _get_next_credential(
                                credential_manager, payload, use_public_api, target_url
                            )
                            if next_cred_result:
                                current_file, credential_data, headers, final_post_data, target_url = next_cred_result

                            await asyncio.sleep(0.5)  # 短暂延迟后重试
                            continue  # 继续循环重试

                        # 如果不是自动封禁错误，或已达到最大重试次数，返回错误
                        # 处理凭证轮换（记录错误并禁用凭证）
                        await _handle_api_error(
                            credential_manager, resp.status_code, response_content, current_file
                        )

                        # [FIX] 返回普通 Response 而不是 StreamingResponse
                        # 避免抗截断机制误判错误响应为需要续写的截断内容
                        return _create_error_response(
                            f"API error: {resp.status_code}", resp.status_code
                        )
                    else:
                        # 成功响应，传递所有资源给流式处理函数管理
                        return _handle_streaming_response_managed(
                            resp,
                            stream_ctx,
                            client,
                            credential_manager,
                            payload.get("model", ""),
                            current_file,
                        )

                except Exception as e:
                    # 清理资源
                    try:
                        await client.aclose()
                    except:
                        pass
                    raise e

            else:
                # 非流式请求处理 - 使用httpx_client模块
                async with http_client.get_client(timeout=None) as client:
                    resp = await client.post(target_url, content=final_post_data, headers=headers)

                    if resp.status_code == 429:
                        # 获取响应内容用于解析冷却时间
                        response_content = resp.text
                        
                        # 解析冷却时间并记录429错误
                        cooldown_until = _parse_quota_reset_delay(response_content) if response_content else None
                        if credential_manager and current_file:
                            await credential_manager.record_api_call_result(
                                current_file, False, 429, cooldown_until=cooldown_until
                            )

                        # 如果重试可用且未达到最大次数，继续重试
                        if retry_429_enabled and attempt < max_retries:
                            if credential_manager:
                                # 429错误时强制轮换凭证，不增加调用计数
                                await credential_manager.force_rotate_credential()
                                # 获取下一个凭证
                                next_cred_result = await _get_next_credential(
                                    credential_manager, payload, use_public_api, target_url
                                )
                                if next_cred_result:
                                    current_file, credential_data, headers, final_post_data, target_url = next_cred_result
                                    # [FIX] 成功切换到新凭证，立即重试（新凭证可能有配额）
                                    log.info(f"[RETRY] 429 error, switched to new credential, retrying immediately ({attempt + 1}/{max_retries})")
                                    continue
                                else:
                                    # 没有其他可用凭证，指数退避
                                    delay = retry_interval * (2 ** attempt)
                                    log.warning(
                                        f"[RETRY] 429 error, no other credentials available, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                                    )
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                # 没有凭证管理器，指数退避
                                delay = retry_interval * (2 ** attempt)
                                log.warning(
                                    f"[RETRY] 429 error encountered, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                                )
                                await asyncio.sleep(delay)
                                continue
                        else:
                            log.error(f"[RETRY] Max retries exceeded for 429 error")
                            return _create_error_response(
                                "429 rate limit exceeded, max retries reached", 429
                            )
                    else:
                        # 非429错误或成功响应，检查是否需要重试
                        if resp.status_code != 200:
                            # 5xx 服务器错误：不切换凭证，直接等待重试
                            if 500 <= resp.status_code < 600 and attempt < max_retries:
                                # 指数退避：base_delay * 2^attempt (1s, 2s, 4s, 8s...)
                                delay = retry_interval * (2 ** attempt)
                                log.warning(
                                    f"[RETRY] Server error {resp.status_code}, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries})"
                                )
                                await asyncio.sleep(delay)
                                continue  # 使用同一凭证重试

                            # [FIX] 400 错误是参数错误，直接返回，不刷新 token
                            if resp.status_code == 400:
                                log.error(
                                    f"[BAD REQUEST] Invalid request parameters. Response: {resp.text[:500] if hasattr(resp, 'text') else 'N/A'}"
                                )
                                return _create_error_response(
                                    f"Invalid request: {resp.text if hasattr(resp, 'text') else 'Bad Request'}", 400
                                )

                            # 检查是否是自动封禁错误码（403, 401, 404）
                            should_auto_ban = await _check_should_auto_ban(resp.status_code)

                            if should_auto_ban and credential_manager and attempt < max_retries:
                                # 记录错误
                                if current_file:
                                    await credential_manager.record_api_call_result(
                                        current_file, False, resp.status_code
                                    )

                                # 401/404 错误：先尝试刷新 token（可能是 token 过期）
                                if resp.status_code in (401, 404):
                                    log.warning(
                                        f"[AUTH REFRESH] {resp.status_code} error, attempting token refresh before retry ({attempt + 1}/{max_retries})"
                                    )
                                    refresh_success = await credential_manager.force_refresh_current_token()

                                    if refresh_success:
                                        # Token 刷新成功，使用同一凭证重试
                                        log.info("[AUTH REFRESH] Token refreshed, retrying with same credential")
                                        # 重新准备请求头（使用刷新后的 token）
                                        next_cred_result = await _get_next_credential(
                                            credential_manager, payload, use_public_api, target_url
                                        )
                                        if next_cred_result:
                                            current_file, credential_data, headers, final_post_data, target_url = next_cred_result
                                        await asyncio.sleep(0.5)
                                        continue  # 重试

                                    # Token 刷新失败，继续走封禁流程
                                    log.warning("[AUTH REFRESH] Token refresh failed, proceeding with credential ban")

                                # 403 或 token 刷新失败：封禁当前凭证并切换到下一个凭证重试
                                log.warning(
                                    f"[RETRY] {resp.status_code} error encountered, rotating credential and retrying ({attempt + 1}/{max_retries})"
                                )
                                # 禁用当前凭证并轮换
                                await _handle_auto_ban(credential_manager, resp.status_code, current_file)

                                # 获取下一个凭证
                                next_cred_result = await _get_next_credential(
                                    credential_manager, payload, use_public_api, target_url
                                )
                                if next_cred_result:
                                    current_file, credential_data, headers, final_post_data, target_url = next_cred_result

                                await asyncio.sleep(0.5)  # 短暂延迟后重试
                                continue  # 继续循环重试

                        # 不是自动封禁错误，或已达到最大重试次数，正常处理
                        return await _handle_non_streaming_response(
                            resp, credential_manager, payload.get("model", ""), current_file
                        )

        except Exception as e:
            if attempt < max_retries:
                # 指数退避：base_delay * 2^attempt (1s, 2s, 4s, 8s...)
                delay = retry_interval * (2 ** attempt)
                log.warning(
                    f"[RETRY] Request failed with exception, waiting {delay:.1f}s before retry ({attempt + 1}/{max_retries}): {str(e)}"
                )
                await asyncio.sleep(delay)
                continue
            else:
                log.error(f"Request to Google API failed: {str(e)}")
                return _create_error_response(f"Request failed: {str(e)}")

    # 如果循环结束仍未成功，返回错误
    return _create_error_response("Max retries exceeded", 429)


def _handle_streaming_response_managed(
    resp,
    stream_ctx,
    client,
    credential_manager: CredentialManager = None,
    model_name: str = "",
    current_file: str = None,
) -> StreamingResponse:
    """Handle streaming response with complete resource lifecycle management."""

    # 检查HTTP错误
    if resp.status_code != 200:
        # 立即清理资源并返回错误
        async def cleanup_and_error():
            try:
                await stream_ctx.__aexit__(None, None, None)
            except:
                pass
            try:
                await client.aclose()
            except:
                pass

            # 获取响应内容用于详细错误显示
            response_content = ""
            try:
                content_bytes = await resp.aread()
                if isinstance(content_bytes, bytes):
                    response_content = content_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                log.debug(f"[STREAMING] Failed to read response content for error analysis: {e}")
                response_content = ""

            # 显示详细错误信息
            if resp.status_code == 429:
                if response_content:
                    log.error(
                        f"Google API returned status 429 (STREAMING). Response details: {response_content[:500]}"
                    )
                else:
                    log.error(f"Google API returned status 429 (STREAMING)")
            else:
                if response_content:
                    log.error(
                        f"Google API returned status {resp.status_code} (STREAMING). Response details: {response_content[:500]}"
                    )
                else:
                    log.error(f"Google API returned status {resp.status_code} (STREAMING)")

            # 记录API调用错误
            if credential_manager and current_file:
                await credential_manager.record_api_call_result(
                    current_file, False, resp.status_code
                )

            await _handle_api_error(credential_manager, resp.status_code, response_content, current_file)

            # [FIX] 返回普通 Response 而不是 StreamingResponse
            # 避免抗截断机制误判错误响应为需要续写的截断内容
            error_response = {
                "error": {
                    "message": f"API error: {resp.status_code}",
                    "type": "api_error",
                    "code": resp.status_code,
                }
            }
            return Response(
                content=json.dumps(error_response),
                status_code=resp.status_code,
                media_type="application/json",
            )

        # 不再使用 StreamingResponse 返回错误
        # return StreamingResponse(
        #     cleanup_and_error(), media_type="text/event-stream", status_code=resp.status_code
        # )

    # 正常流式响应处理，确保资源在流结束时被清理
    async def managed_stream_generator():
        success_recorded = False
        managed_stream_generator._chunk_count = 0  # 初始化chunk计数器
        try:
            async for chunk in resp.aiter_lines():
                if not chunk or not chunk.startswith("data: "):
                    continue

                # 记录第一次成功响应
                if not success_recorded:
                    if current_file and credential_manager:
                        await credential_manager.record_api_call_result(current_file, True)
                    success_recorded = True

                payload = chunk[len("data: ") :]
                try:
                    obj = json.loads(payload)
                    if "response" in obj:
                        data = obj["response"]
                        yield f"data: {json.dumps(data, separators=(',',':'))}\n\n".encode()
                        await asyncio.sleep(0)  # 让其他协程有机会运行

                        # 定期释放内存（每100个chunk）
                        if hasattr(managed_stream_generator, "_chunk_count"):
                            managed_stream_generator._chunk_count += 1
                            if managed_stream_generator._chunk_count % 100 == 0:
                                gc.collect()
                    else:
                        yield f"data: {json.dumps(obj, separators=(',',':'))}\n\n".encode()
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            log.error(f"Streaming error: {e}")
            err = {"error": {"message": str(e), "type": "api_error", "code": 500}}
            yield f"data: {json.dumps(err)}\n\n".encode()
        finally:
            # 确保清理所有资源
            try:
                await stream_ctx.__aexit__(None, None, None)
            except Exception as e:
                log.debug(f"Error closing stream context: {e}")
            try:
                await client.aclose()
            except Exception as e:
                log.debug(f"Error closing client: {e}")

    return StreamingResponse(managed_stream_generator(), media_type="text/event-stream")


async def _handle_non_streaming_response(
    resp,
    credential_manager: CredentialManager = None,
    model_name: str = "",
    current_file: str = None,
) -> Response:
    """Handle non-streaming response from Google API."""
    if resp.status_code == 200:
        try:
            # 记录成功响应
            if current_file and credential_manager:
                await credential_manager.record_api_call_result(current_file, True)

            raw = await resp.aread()
            google_api_response = raw.decode("utf-8")
            if google_api_response.startswith("data: "):
                google_api_response = google_api_response[len("data: ") :]
            google_api_response = json.loads(google_api_response)
            log.debug(
                f"Google API原始响应: {json.dumps(google_api_response, ensure_ascii=False)[:500]}..."
            )
            standard_gemini_response = google_api_response.get("response")
            log.debug(
                f"提取的response字段: {json.dumps(standard_gemini_response, ensure_ascii=False)[:500]}..."
            )
            return Response(
                content=json.dumps(standard_gemini_response),
                status_code=200,
                media_type="application/json; charset=utf-8",
            )
        except Exception as e:
            log.error(f"Failed to parse Google API response: {str(e)}")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("Content-Type"),
            )
    else:
        # 获取响应内容用于详细错误显示
        response_content = ""
        try:
            if hasattr(resp, "content"):
                content = resp.content
                if isinstance(content, bytes):
                    response_content = content.decode("utf-8", errors="ignore")
            else:
                content_bytes = await resp.aread()
                if isinstance(content_bytes, bytes):
                    response_content = content_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            log.debug(f"[NON-STREAMING] Failed to read response content for error analysis: {e}")
            response_content = ""

        # 显示详细错误信息
        if resp.status_code == 429:
            if response_content:
                log.error(
                    f"Google API returned status 429 (NON-STREAMING). Response details: {response_content[:500]}"
                )
            else:
                log.error(f"Google API returned status 429 (NON-STREAMING)")
        else:
            if response_content:
                log.error(
                    f"Google API returned status {resp.status_code} (NON-STREAMING). Response details: {response_content[:500]}"
                )
            else:
                log.error(f"Google API returned status {resp.status_code} (NON-STREAMING)")

        # 记录API调用错误
        if credential_manager and current_file:
            await credential_manager.record_api_call_result(current_file, False, resp.status_code)

        await _handle_api_error(credential_manager, resp.status_code, response_content, current_file)

        return _create_error_response(f"API error: {resp.status_code}", resp.status_code)


def build_gemini_payload_from_native(native_request: dict, model_from_path: str) -> dict:
    """
    Build a Gemini API payload from a native Gemini request with full pass-through support.
    """
    # 创建请求副本以避免修改原始数据
    request_data = native_request.copy()

    # 合并安全设置（增量补充用户未配置的默认设置，避免覆盖用户自定义设置）
    request_data["safetySettings"] = _merge_safety_settings(
        request_data.get("safetySettings")
    )

    # 确保generationConfig存在
    if "generationConfig" not in request_data:
        request_data["generationConfig"] = {}

    generation_config = request_data["generationConfig"]

    # 配置thinking（如果未指定thinkingConfig）
    if "thinkingConfig" not in generation_config:
        generation_config["thinkingConfig"] = {}

    thinking_config = generation_config["thinkingConfig"]

    # 只有在未明确设置时才应用默认thinking配置
    if "includeThoughts" not in thinking_config:
        thinking_config["includeThoughts"] = should_include_thoughts(model_from_path)
    if "thinkingBudget" not in thinking_config:
        thinking_config["thinkingBudget"] = get_thinking_budget(model_from_path)

    # 为搜索模型添加Google Search工具（如果未指定且没有functionDeclarations）
    if is_search_model(model_from_path):
        if "tools" not in request_data:
            request_data["tools"] = []
        # 检查是否已有functionDeclarations或googleSearch工具
        has_function_declarations = any(
            tool.get("functionDeclarations") for tool in request_data["tools"]
        )
        has_google_search = any(tool.get("googleSearch") for tool in request_data["tools"])

        # 只有在没有任何工具时才添加googleSearch，或者只有googleSearch工具时可以添加更多googleSearch
        if not has_function_declarations and not has_google_search:
            request_data["tools"].append({"googleSearch": {}})

    # 透传所有其他Gemini原生字段:
    # - contents (必需)
    # - systemInstruction (可选)
    # - generationConfig (已处理)
    # - safetySettings (已处理)
    # - tools (已处理)
    # - toolConfig (透传)
    # - cachedContent (透传)
    # - 以及任何其他未知字段都会被透传

    return {"model": get_base_model_name(model_from_path), "request": request_data}
