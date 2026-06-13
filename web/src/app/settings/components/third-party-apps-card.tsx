"use client";

import { useEffect, useState } from "react";
import { ExternalLink, LoaderCircle, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import webConfig from "@/constants/common-env";
import { getStoredAuthSession } from "@/store/auth";

import { useSettingsStore } from "../store";

const usableModels = ["gpt-image-2", "codex-gpt-image-2", "auto", "gpt-5", "gpt-5-1", "gpt-5-2", "gpt-5-3", "gpt-5-3-mini", "gpt-5-mini"];

const endpoints = [
  "GET /v1/models",
  "POST /v1/chat/completions",
  "POST /v1/responses",
  "POST /v1/images/generations",
  "POST /v1/images/edits",
];

export function ThirdPartyAppsCard() {
  const [authKey, setAuthKey] = useState("");
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setInfiniteCanvasField = useSettingsStore((state) => state.setInfiniteCanvasField);
  const saveConfig = useSettingsStore((state) => state.saveConfig);

  useEffect(() => {
    let active = true;
    void getStoredAuthSession().then((session) => {
      if (active) {
        setAuthKey(session?.key || "");
      }
    });
    return () => {
      active = false;
    };
  }, []);

  if (isLoadingConfig || !config?.third_party_apps) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex items-center justify-center p-10">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </CardContent>
      </Card>
    );
  }

  const canvas = config.third_party_apps.infinite_canvas;
  const serviceBaseUrl = webConfig.apiUrl.replace(/\/$/, "") || (typeof window !== "undefined" ? window.location.origin : "");
  const openAIBaseUrl = `${serviceBaseUrl}/v1`;
  const displayKey = authKey || "<当前密钥>";
  const chatExample = `curl ${openAIBaseUrl}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${displayKey}" \\
  -d '{"model":"gpt-5-mini","messages":[{"role":"user","content":"你好"}]}'`;
  const imageExample = `curl ${openAIBaseUrl}/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${displayKey}" \\
  -d '{"model":"gpt-image-2","prompt":"一张极简产品海报","n":1}'`;

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-5 p-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="flex items-center gap-2 text-base font-semibold text-stone-900">
              <ExternalLink className="size-5 text-stone-500" />
              三方应用
            </div>
            <p className="mt-1 text-xs leading-6 text-stone-500">开启后会在顶部导航显示入口，跳转时自动附带本项目地址和当前密钥。</p>
          </div>
          <span className={`rounded-full px-3 py-1 text-xs ${canvas.enabled ? "bg-emerald-50 text-emerald-700" : "bg-stone-100 text-stone-500"}`}>
            {canvas.enabled ? "已启用" : "未启用"}
          </span>
        </div>

        <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-3">
          <label className="flex items-center gap-3 text-sm text-stone-700">
            <Checkbox
              checked={Boolean(canvas.enabled)}
              onCheckedChange={(checked) => setInfiniteCanvasField("enabled", Boolean(checked))}
            />
            启用无限画布
          </label>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">无限画布地址</label>
            <Input
              value={canvas.url}
              onChange={(event) => setInfiniteCanvasField("url", event.target.value)}
              placeholder="https://canvas.best"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs leading-5 text-stone-500">
              顶部入口跳转时会追加 apiKey 和 baseUrl 参数；关闭后顶部导航不显示无限画布。
            </p>
            <p className="text-xs leading-5 text-amber-700">
              该入口仅供个人测试使用；长期使用建议自行本机部署无限画布。
            </p>
          </div>
        </div>

        <div className="space-y-4 rounded-xl border border-stone-200 bg-stone-50 px-4 py-4">
          <div>
            <div className="text-sm font-semibold text-stone-900">接入其他第三方应用</div>
            <p className="mt-1 text-xs leading-6 text-stone-500">按 OpenAI 兼容接口填写。Base URL 填 OpenAI Base URL，API Key 填当前密钥，请求头使用 Authorization: Bearer 当前密钥；如果应用会自动拼接 /v1，则 Base URL 填服务地址。</p>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
              <div className="text-xs text-stone-500">服务地址</div>
              <div className="break-all font-mono text-xs text-stone-800">{serviceBaseUrl}</div>
            </div>
            <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
              <div className="text-xs text-stone-500">Base URL（OpenAI）</div>
              <div className="break-all font-mono text-xs text-stone-800">{openAIBaseUrl}</div>
            </div>
            <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
              <div className="text-xs text-stone-500">API Key（当前密钥）</div>
              <div className="break-all font-mono text-xs text-stone-800">{displayKey}</div>
            </div>
            <div className="space-y-1 rounded-xl border border-stone-200 bg-white px-3 py-2">
              <div className="text-xs text-stone-500">请求头</div>
              <div className="break-all font-mono text-xs text-stone-800">Authorization: Bearer {displayKey}</div>
            </div>
          </div>

          <div className="space-y-2">
            <div className="text-xs font-medium text-stone-600">常用模型（也可请求 /v1/models 获取）</div>
            <div className="flex flex-wrap gap-2">
              {usableModels.map((model) => (
                <span key={model} className="rounded-md border border-stone-200 bg-white px-2 py-1 font-mono text-xs text-stone-700">{model}</span>
              ))}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2">
              <div className="text-xs font-medium text-stone-600">可用接口</div>
              <div className="space-y-1">
                {endpoints.map((endpoint) => (
                  <div key={endpoint} className="rounded-lg bg-white px-3 py-2 font-mono text-xs text-stone-700">{endpoint}</div>
                ))}
              </div>
            </div>
            <div className="space-y-2">
              <div className="text-xs font-medium text-stone-600">调用示例</div>
              <pre className="max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-stone-950 px-3 py-3 text-xs leading-5 text-stone-100">{chatExample}</pre>
              <pre className="max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-stone-950 px-3 py-3 text-xs leading-5 text-stone-100">{imageExample}</pre>
            </div>
          </div>
        </div>

        <div className="flex justify-end">
          <Button className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800" onClick={() => void saveConfig()} disabled={isSavingConfig}>
            {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
