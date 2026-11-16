// AI 文字只經 AiText（Story 3.8、NFR-2）：HTML 標籤、javascript: 連結、Markdown 連結與圖片、腳本樣的字串，
// 渲染出來都不會變成可點或可執行的東西；只留段落、粗體、清單。
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AiText } from "./AiText";

function render(text: string, inline = false): string {
  return renderToStaticMarkup(<AiText text={text} inline={inline} />);
}

/** 渲染結果裡所有的元素名稱。 */
function tags(html: string): string[] {
  return [...html.matchAll(/<([a-z][a-z0-9]*)\b/g)].map((match) => match[1] ?? "");
}

const ATTACKS = [
  "<script>window.__xss = 1</script>",
  '<img src=x onerror="window.__xss = 2">',
  '<a href="javascript:window.__xss = 3">link</a>',
  "[link](javascript:window.__xss=4)",
  "[link](https://example.com/phish)",
  "<https://example.com/autolink>",
  "![image](https://example.com/x.png)",
  '<iframe src="https://example.com"></iframe>',
  "<style>body{display:none}</style>",
  '<svg onload="window.__xss = 5"></svg>',
  '<b onclick="window.__xss = 6">bold</b>',
  "[ref]: javascript:window.__xss=7\n\n[click][ref]",
];

describe("AiText", () => {
  it.each(ATTACKS)("leaves nothing clickable or executable: %s", (attack) => {
    for (const inline of [false, true]) {
      const html = render(`before ${attack} after`, inline);
      for (const tag of tags(html)) {
        expect(["div", "span", "p", "strong", "ul", "ol", "li"]).toContain(tag);
      }
      // 外層只有 data-ai-text（與呼叫端給的 class、lang）；裡面的元素沒有任何屬性。
      expect(html).not.toMatch(/\s(href|src|on[a-z]+|style|action|formaction|xlink:href)=/i);
    }
  });

  it("keeps the words of a Markdown link but not the link", () => {
    const html = render("see [the paper](javascript:alert(1)) here");
    expect(html).toBe("<div data-ai-text=\"true\"><p>see the paper here</p></div>");
  });

  it("shows script-looking text as plain text", () => {
    const text = "{{constructor.constructor('return 1')()}} javascript:alert(1) ${window.x}";
    const html = render(text);
    expect(html).toContain("{{constructor.constructor(&#x27;return 1&#x27;)()}}");
    expect(html).toContain("javascript:alert(1)");
    expect(tags(html)).toEqual(["div", "p"]);
  });

  it("keeps paragraphs, bold and lists", () => {
    expect(tags(render("one **two**\n\nthree"))).toEqual(["div", "p", "strong", "p"]);
    expect(tags(render("- a\n- b"))).toEqual(["div", "ul", "li", "li"]);
    expect(tags(render("1. a\n2. b"))).toEqual(["div", "ol", "li", "li"]);
  });

  it("inline: a span with bold only, no paragraphs or lists", () => {
    expect(render("name **bold**", true)).toBe('<span data-ai-text="true">name <strong>bold</strong></span>');
    expect(tags(render("- a\n- b", true))).toEqual(["span"]);
  });

  it("passes class, language and paragraph class through", () => {
    const html = renderToStaticMarkup(<AiText text="quote" className="outer" lang="en" paragraphClassName="inner" />);
    expect(html).toBe('<div data-ai-text="true" class="outer" lang="en"><p class="inner">quote</p></div>');
  });
});
