/**
 * `media-worker.mjs`（数据仓库的 Worker 脚本，D148 追补）里 `sliceRange` 的单元测试。
 *
 *     node --test tools/tests/media_worker.test.mjs
 *
 * 为什么单独测这个纯函数：它是**唯一自己写的协议解析**（`Range` 头 → 闭区间），
 * 而它错了的表现是"音频播不出来 / 播一半"这种很难归因的故障。Worker 本体（`env.ASSETS` 那一层）
 * 只能在 CF 上跑，但这段解析完全可以在本地钉死。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { sliceRange } from "../../media-worker.mjs";

const SIZE = 1000;

test("bytes=a-b：闭区间（含端点）", () => {
  assert.deepEqual(sliceRange("bytes=0-99", SIZE), [0, 99]);
  assert.deepEqual(sliceRange("bytes=500-500", SIZE), [500, 500]);
});

test("bytes=a-：到结尾", () => {
  assert.deepEqual(sliceRange("bytes=900-", SIZE), [900, 999]);
  assert.deepEqual(sliceRange("bytes=0-", SIZE), [0, 999]);
});

test("bytes=-n：最后 n 字节", () => {
  assert.deepEqual(sliceRange("bytes=-100", SIZE), [900, 999]);
  assert.deepEqual(sliceRange("bytes=-1000", SIZE), [0, 999]);   // n 比文件大 ⇒ 整份
});

test("超出结尾就夹到结尾（HTTP 允许）", () => {
  assert.deepEqual(sliceRange("bytes=990-99999", SIZE), [990, 999]);
});

test("越界 / 倒挂 / 乱写 ⇒ null（调用方回 416）", () => {
  assert.equal(sliceRange("bytes=1000-", SIZE), null);          // 起点越界
  assert.equal(sliceRange("bytes=1200-1300", SIZE), null);
  assert.equal(sliceRange("bytes=500-100", SIZE), null);        // 倒挂
  assert.equal(sliceRange("bytes=-0", SIZE), null);             // 后缀 0 = 空
  assert.equal(sliceRange("bytes=-", SIZE), null);
  assert.equal(sliceRange("items=0-99", SIZE), null);           // 单位不是 bytes
  assert.equal(sliceRange("", SIZE), null);
  assert.equal(sliceRange(undefined, SIZE), null);
  assert.equal(sliceRange("bytes=0-99", 0), null);              // 空文件
});

test("多段只取第一段（我们只回单段 206）", () => {
  assert.deepEqual(sliceRange("bytes=0-9, 20-29", SIZE), [0, 9]);
});

test("容忍空白与大小写之外的空隙（浏览器实际会发的两种形态）", () => {
  assert.deepEqual(sliceRange(" bytes=0-99 ", SIZE), [0, 99]);
});
