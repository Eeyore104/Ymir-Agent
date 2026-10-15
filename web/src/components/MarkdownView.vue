<!--
  Markdown 渲染（产物内容查看：PRD / 设计文档 / 评测报告）。

  使用 markdown-it（html: false，原始 HTML 转义，防 XSS）。
-->
<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'

interface Props {
  /** Markdown 原文；为空渲染空态。 */
  content: string | null
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  content: null,
  loading: false,
})

/** 模块级单例（markdown-it 实例无状态，可复用）。 */
const md = new MarkdownIt({
  html: false, // 不解析原始 HTML（转义输出，防注入）
  linkify: true,
  breaks: true,
  typographer: true,
})

const html = computed<string>(() => {
  if (!props.content) return ''
  return md.render(props.content)
})
</script>

<template>
  <div v-loading="loading" class="markdown-view">
    <div v-if="!content" class="markdown-empty">暂无内容</div>
    <div v-else class="markdown-body" v-html="html" />
  </div>
</template>

<style scoped>
.markdown-view {
  min-height: 120px;
}

.markdown-empty {
  color: #909399;
  text-align: center;
  padding: 32px 0;
  font-size: 13px;
}

.markdown-body {
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
  word-break: break-word;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 16px 0 8px;
  font-weight: 600;
  line-height: 1.4;
}

.markdown-body :deep(h1) {
  font-size: 20px;
}

.markdown-body :deep(h2) {
  font-size: 17px;
}

.markdown-body :deep(h3) {
  font-size: 15px;
}

.markdown-body :deep(p) {
  margin: 8px 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 24px;
  margin: 8px 0;
}

.markdown-body :deep(code) {
  background: #f2f3f5;
  border-radius: 3px;
  padding: 2px 5px;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 0.92em;
  color: #c7254e;
}

.markdown-body :deep(pre) {
  background: #282c34;
  color: #abb2bf;
  border-radius: 6px;
  padding: 12px 16px;
  overflow-x: auto;
  margin: 10px 0;
}

.markdown-body :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
}

.markdown-body :deep(blockquote) {
  border-left: 4px solid #dcdfe6;
  margin: 8px 0;
  padding: 4px 12px;
  color: #606266;
  background: #fafafa;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  width: 100%;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #dcdfe6;
  padding: 6px 10px;
  font-size: 13px;
}

.markdown-body :deep(th) {
  background: #f5f7fa;
}

.markdown-body :deep(a) {
  color: #409eff;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid #ebeef5;
  margin: 14px 0;
}
</style>
