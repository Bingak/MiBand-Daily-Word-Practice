// common/wordLoader.js
//
// 词库惰性加载器
// ================
// 背景：早期版本把 5800+ 个单词写成 `export const words = [{...},{...}]` 的对象数组，
// 模块被 import 时 JS 引擎需要一次性构造上万个 JS 对象与字符串，再加 learn 页里的
// filter / spread / shuffle 又克隆出 2~3 份引用数组，内存峰值直接击穿手环快应用的堆上限，
// 表现为「安装到手环后设备重启」。
//
// 本加载器的策略：
//   1. 词库以「分片 + 紧凑字符串」存储（tools/build_words.py 生成），
//      模块顶层只有字符串字面量，不构造任何词条对象，启动开销极低。
//   2. 抽词时按需 split 单个分片（约 200 行），取完立刻释放，
//      峰值内存 ≈ 1 个分片，而不是整本词库。
//   3. 从多个随机分片中按配额取词，兼顾「随机性」与「内存可控」。

import RAW_PARTS, { META } from './words/index.js';

// 字段分隔符，与 tools/build_words.py 中的 FIELD_SEP 保持一致
const FIELD_SEP = '\u0001';

/**
 * 生成 0..n-1 的随机排列
 */
function shuffleIdx(n) {
  const arr = new Array(n);
  for (let i = 0; i < n; i++) {
    arr[i] = i;
  }
  for (let i = n - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const t = arr[i];
    arr[i] = arr[j];
    arr[j] = t;
  }
  return arr;
}

/**
 * 从 partCount 个分片中随机挑 k 个（不重复），避免每次都集中在同一段字母序区间
 */
function pickRandomParts(partCount, k) {
  const idx = new Array(partCount);
  for (let i = 0; i < partCount; i++) {
    idx[i] = i;
  }
  const n = Math.min(k, partCount);
  for (let i = 0; i < n; i++) {
    const j = i + Math.floor(Math.random() * (partCount - i));
    const t = idx[i];
    idx[i] = idx[j];
    idx[j] = t;
  }
  idx.length = n;
  return idx;
}

/**
 * 把一行紧凑数据解析成词条对象
 * 行格式：english \u0001 phonetic \u0001 chinese
 */
function parseLine(line) {
  const s1 = line.indexOf(FIELD_SEP);
  if (s1 < 0) {
    return { english: line, phonetic: '', chinese: '' };
  }
  const s2 = line.indexOf(FIELD_SEP, s1 + 1);
  if (s2 < 0) {
    return {
      english: line.substring(0, s1),
      phonetic: '',
      chinese: line.substring(s1 + 1)
    };
  }
  return {
    english: line.substring(0, s1),
    phonetic: line.substring(s1 + 1, s2),
    chinese: line.substring(s2 + 1)
  };
}

/**
 * 从单个分片中随机挑出至多 limit 条未排除的词
 * 注意：lines 在函数返回前被清空，帮助 GC 及时回收
 */
function collectFromPart(partIndex, limit, exclude, pickedMap, out) {
  if (limit <= 0) return;
  const raw = RAW_PARTS[partIndex];
  if (!raw) return;

  const lines = raw.split('\n');
  const order = shuffleIdx(lines.length);

  for (let j = 0; j < order.length && limit > 0; j++) {
    const line = lines[order[j]];
    if (!line) continue;
    const s1 = line.indexOf(FIELD_SEP);
    if (s1 < 0) continue;
    const eng = line.substring(0, s1);
    // 同一批次内去重 + 跳过已标记为「熟知」的词
    if (pickedMap[eng] || exclude[eng]) continue;
    pickedMap[eng] = true;
    out.push(parseLine(line));
    limit--;
  }

  // 主动释放这一片的解析结果，峰值内存始终只有 1 个分片
  lines.length = 0;
  order.length = 0;
}

/**
 * 抽取当日学习词表
 *
 * @param {number} count      需要的词条数量（由用户设置的「每日背诵数量」决定）
 * @param {object} excludeMap 需要排除的单词集合，形如 { 'abandon': true }
 * @returns {Array<{english:string, phonetic:string, chinese:string}>}
 */
export function pickDaily(count, excludeMap) {
  const total = META.total;
  const partCount = META.partCount;
  if (!total || !count || count <= 0) return [];

  const want = Math.min(count, total);
  const exclude = excludeMap || {};
  const picked = [];
  const pickedMap = {};

  // 访问的分片数量：越多则字母序上越分散，但 CPU 开销略增。
  // 由于每片解析后立即释放，内存峰值与分片数量无关。
  const partNum = Math.min(partCount, Math.max(6, Math.ceil(want / 10)));
  const parts = pickRandomParts(partCount, partNum);

  // 第一轮：每个分片按配额取，保证字母序上均匀铺开
  const quota = Math.ceil(want / parts.length);
  for (let i = 0; i < parts.length && picked.length < want; i++) {
    // 注意按「还差多少」收敛单片上限，否则最后一片会超额
    collectFromPart(parts[i], Math.min(quota, want - picked.length),
      exclude, pickedMap, picked);
  }

  // 第二轮：若因排除过多没取满，继续从随机分片补齐
  if (picked.length < want) {
    const rest = pickRandomParts(partCount, partNum);
    for (let i = 0; i < rest.length && picked.length < want; i++) {
      collectFromPart(rest[i], want - picked.length, exclude, pickedMap, picked);
    }
  }

  return picked;
}

/**
 * 词库总词条数
 */
export function getTotal() {
  return META.total;
}

/**
 * 词库元信息：{ total, partCount, perPart, partSizes }
 */
export function getMeta() {
  return META;
}
