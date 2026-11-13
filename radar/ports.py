"""Port（核心訂的介面，用 Protocol 寫）。

全專案只有四個：PaperSource、LlmProvider、EmbeddingProvider、ObjectStore（ADR-0001）。
各自由第一個用到的 Story 加進來；還沒有用到的不先建。
"""
