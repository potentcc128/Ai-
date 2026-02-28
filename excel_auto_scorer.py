#!/usr/bin/env python3
"""
Excel批量自动评分处理器 V4
- 使用V4流水线（5阶段递进式评分）
- 支持断点续传、错误重试、进度保存
- 支持并行处理（可选）
"""

import pandas as pd
import json
import os
import time
import logging
from datetime import datetime
from typing import Dict, Optional, List
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from auto_scoring_pipeline import AutoScoringPipeline


class ExcelAutoScorer:
    """Excel自动评分处理器 V4"""

    def __init__(self, config_file: str, checkpoint_dir: str = None, enable_search: bool = True):
        """
        初始化

        Args:
            config_file: 配置文件路径
            checkpoint_dir: checkpoint目录（默认使用临时目录）
            enable_search: 是否启用搜索验证
        """
        self.logger = logging.getLogger("ExcelAutoScorer")

        # 初始化V4评分流水线
        self.pipeline = AutoScoringPipeline(config_file, enable_search=enable_search)

        # 创建checkpoint目录
        if checkpoint_dir is None:
            checkpoint_dir = './checkpoints'
        self.checkpoint_dir = checkpoint_dir
        Path(self.checkpoint_dir).mkdir(parents=True, exist_ok=True)

        self.logger.info(f"✓ 使用V4流水线")
        self.logger.info(f"✓ Checkpoint目录: {self.checkpoint_dir}")

    def _extract_row_data(self, row) -> Dict:
        """从行中提取数据"""
        return {
            'user_question': str(row.get('用户问题', row.get('query', row.get('问题', '')))),
            'original': str(row.get('Original', row.get('original', row.get('答案', ''))))
        }

    def _format_layer1(self, layer1: Optional[Dict]) -> str:
        """格式化第一层结果（底线检查）"""
        if not layer1:
            return "未检查"
        if layer1.get('has_fatal_issue'):
            return f"❌ {layer1.get('issue_type', '未知')} - {layer1.get('detail', '')[:80]}"
        return "✓ 通过"

    def _format_layer2(self, layer2: Optional[Dict]) -> str:
        """格式化第二层结果（信息点验证）"""
        if not layer2:
            return "未检查"
        stats = layer2.get('stats', {})
        total = stats.get('total', 0)
        verified_true = stats.get('verified_true', 0)
        verified_false = stats.get('verified_false', 0)
        verified_null = stats.get('verified_null', 0)
        critical_false = stats.get('critical_false', 0)
        critical_total = stats.get('critical_total', 0)
        base = f"通过:{verified_true} 失败:{verified_false} 无法验证:{verified_null} (共{total})"
        if critical_false > 0:
            base += f" ⚠️关键失败:{critical_false}"
        return base

    def _format_critical_claims(self, layer2: Optional[Dict]) -> str:
        """导出关键信息点的验证结果（failed/null）"""
        if not layer2:
            return ""
        all_v = layer2.get('all_verifications', [])
        lines = []
        for item in all_v:
            claim_dict = item.get('claim', {})
            if not claim_dict.get('critical'):
                continue
            verified = item.get('verify_result', {}).get('verified')
            reason = item.get('verify_result', {}).get('reason', '')
            claim_text = claim_dict.get('claim', '')
            if verified is False:
                lines.append(f"❌{claim_text} ({reason})")
            elif verified is None:
                lines.append(f"⚠️{claim_text} ({reason})")
        return "\n".join(lines)

    def _format_layer3(self, layer3: Optional[Dict]) -> str:
        """格式化第三层结果（质量评估）"""
        if not layer3:
            return "未执行"
        satisfaction = (layer3.get('satisfaction') or {}).get('satisfaction_level', '未知')
        score = (layer3.get('quality') or {}).get('final_score', '未知')
        return f"满足度:{satisfaction}, 质量:{score}分"

    def _format_reflection(self, reflection: Optional[Dict]) -> str:
        """格式化反思结果"""
        if not reflection:
            return "未执行"
        if reflection.get('needs_correction'):
            return f"⚠️ 需修正：{reflection.get('correction_reason', '')[:80]}"
        return f"✓ 确认{reflection.get('final_confirmed_score', '?')}分 (置信度:{reflection.get('confidence', '?')})"

    def _format_parsing(self, parsing: Optional[Dict]) -> str:
        """格式化拆分结果（V4格式）"""
        if not parsing:
            return "未拆分"
        total = parsing.get('total_claims', 0)
        stats = parsing.get('stats') or {}
        obj = stats.get('objective', 0)
        subj = stats.get('subjective', 0)
        mixed = stats.get('mixed', 0)
        return f"共{total}点(客观{obj}+主观{subj}+混合{mixed})"

    def _format_claims_detail(self, parsing: Optional[Dict], layer2: Optional[Dict]) -> str:
        """展示每条 claim 及其验证结论"""
        if not parsing:
            return ""
        claims = parsing.get('claims', [])
        if not claims:
            return ""

        # 建立 claim index → 验证结果 的映射（layer2 里按顺序存）
        verify_map = {}
        if layer2:
            for item in layer2.get('all_verifications', []):
                idx = item.get('claim_index', 0)
                vr = item.get('verify_result', {})
                verify_map[idx] = vr

        lines = []
        for i, c in enumerate(claims):
            claim_text = c.get('claim', '')
            critical = '⭐' if c.get('critical') else ''
            vr = verify_map.get(i + 1, {})
            verified = vr.get('verified')
            reason = vr.get('reason', '')
            if verified is True:
                status = '✓'
            elif verified is False:
                status = '✗'
            elif verified is None:
                status = '?'
            else:
                status = '-'
            lines.append(f"{status}{critical} {claim_text}" + (f" [{reason}]" if reason else ""))

        return "\n".join(lines)

    def _format_result_for_excel(self, result: Dict) -> Dict:
        """格式化结果用于Excel写入"""
        layer2 = result.get('layer2_verification')
        parsing = result.get('stage0_parsing')
        return {
            'AI评分': result.get('score'),
            'AI评分理由': result.get('reasoning', ''),
            '底线检查': self._format_layer1(result.get('layer1_baseline')),
            '信息点拆分': self._format_parsing(parsing),
            '拆解详情': self._format_claims_detail(parsing, layer2),
            '信息点验证': self._format_layer2(layer2),
            '关键信息点': self._format_critical_claims(layer2),
            '质量评估': self._format_layer3(result.get('layer3_quality')),
            '反思检查': self._format_reflection(result.get('reflection')),
            '处理时间': result.get('timestamp', datetime.now().isoformat()),
            '耗时(秒)': round(result.get('duration', 0), 1),
            '状态': '✓ 已完成' if result.get('score') is not None else '❌ 失败'
        }

    def _load_checkpoint(self, index: int) -> Optional[Dict]:
        """加载checkpoint"""
        checkpoint_file = os.path.join(self.checkpoint_dir, f"row_{index}.json")
        if os.path.exists(checkpoint_file):
            try:
                with open(checkpoint_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 检查是否是成功的结果
                    if data.get('score') is not None or data.get('error'):
                        return data
            except Exception as e:
                self.logger.warning(f"读取checkpoint失败 (row {index}): {e}")
        return None

    def _save_checkpoint(self, index: int, result: Dict):
        """保存checkpoint"""
        checkpoint_file = os.path.join(self.checkpoint_dir, f"row_{index}.json")
        try:
            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"保存checkpoint失败 (row {index}): {e}")

    def _process_row_with_retry(self, row_data: Dict, index: int, max_retries: int = 2) -> Dict:
        """处理单行数据（带重试）"""

        # 检查checkpoint
        cached_result = self._load_checkpoint(index)
        if cached_result:
            self.logger.info(f"  行 {index+1}: 使用缓存结果 ✓")
            return cached_result

        # 尝试处理
        for attempt in range(max_retries):
            try:
                result = self.pipeline.score(
                    user_question=row_data['user_question'],
                    original=row_data['original']
                )

                # 保存checkpoint
                self._save_checkpoint(index, result)

                return result

            except Exception as e:
                self.logger.error(f"  行 {index+1}: 尝试 {attempt+1}/{max_retries} 失败: {e}")

                if attempt == max_retries - 1:
                    # 最后一次失败，返回错误结果
                    error_result = {
                        'score': None,
                        'reasoning': f"处理失败: {str(e)}",
                        'error': str(e),
                        'timestamp': datetime.now().isoformat(),
                        'duration': 0,
                        'layer1_baseline': None,
                        'stage0_parsing': None,
                        'stage1_searches': None,
                        'layer2_verification': None,
                        'layer3_quality': None,
                        'reflection': None
                    }

                    # 保存错误结果的checkpoint
                    self._save_checkpoint(index, error_result)

                    return error_result

                # 等待后重试
                time.sleep(3)

    def process_excel(self, input_file: str, output_file: str,
                      save_interval: int = 5, start_row: int = 0,
                      parallel: bool = False, max_workers: int = 3) -> pd.DataFrame:
        """
        处理Excel文件

        Args:
            input_file: 输入Excel文件路径
            output_file: 输出Excel文件路径
            save_interval: 每处理N行保存一次
            start_row: 从第几行开始（用于断点续传）
            parallel: 是否并行处理（注意：会增加API并发压力）
            max_workers: 并行线程数（建议2-3，避免超过API速率限制）

        Returns:
            处理后的DataFrame
        """

        self.logger.info("\n" + "="*80)
        self.logger.info("开始批量自动评分 (V4)")
        self.logger.info("="*80)

        # 1. 加载Excel
        self.logger.info(f"📂 加载文件: {input_file}")
        df = pd.read_excel(input_file)
        total_rows = len(df)
        self.logger.info(f"📊 共 {total_rows} 行数据")

        if parallel:
            self.logger.info(f"⚡ 并行模式: {max_workers} 个线程")
        else:
            self.logger.info(f"🔄 顺序模式")

        # 2. 添加输出列（如果不存在）
        output_columns = [
            'AI评分', 'AI评分理由', '底线检查', '信息点拆分',
            '拆解详情', '信息点验证', '关键信息点', '质量评估', '反思检查',
            '处理时间', '耗时(秒)', '状态'
        ]
        for col in output_columns:
            if col not in df.columns:
                df[col] = None

        # 3. 逐行处理
        success_count = 0
        error_count = 0
        start_time = time.time()

        if parallel:
            # 并行处理模式
            self._process_parallel(df, start_row, total_rows, output_file,
                                   save_interval, max_workers)
        else:
            # 顺序处理模式
            for index in range(start_row, total_rows):
                row = df.iloc[index]

                self.logger.info(f"\n{'='*60}")
                self.logger.info(f"处理第 {index+1}/{total_rows} 行")
                self.logger.info(f"{'='*60}")

                # 提取数据
                row_data = self._extract_row_data(row)
                question_preview = row_data['user_question'][:60]
                self.logger.info(f"问题: {question_preview}...")

                # 处理
                try:
                    result = self._process_row_with_retry(row_data, index)

                    # 格式化结果
                    formatted_result = self._format_result_for_excel(result)

                    # 写回DataFrame
                    for col, value in formatted_result.items():
                        df.at[index, col] = value

                    if result.get('score') is not None:
                        score = result['score']
                        duration = result.get('duration', 0)
                        self.logger.info(f"✓ 完成 - 评分: {score}分, 耗时: {duration:.1f}秒")
                        success_count += 1
                    else:
                        self.logger.info(f"✗ 失败 - {result.get('reasoning', '未知错误')}")
                        error_count += 1

                except Exception as e:
                    self.logger.error(f"✗ 异常: {e}", exc_info=True)
                    df.at[index, '状态'] = f"❌ 异常: {str(e)[:100]}"
                    error_count += 1

                # 定期保存
                if (index + 1) % save_interval == 0 or (index + 1) == total_rows:
                    df.to_excel(output_file, index=False)
                    elapsed = time.time() - start_time
                    avg_time = elapsed / (index + 1 - start_row)
                    remaining = (total_rows - index - 1) * avg_time
                    self.logger.info(f"\n💾 已保存进度: {index+1}/{total_rows} 行")
                    self.logger.info(f"⏱️  平均: {avg_time:.1f}秒/条, 预计剩余: {remaining/60:.1f}分钟")

        # 4. 最终保存
        df.to_excel(output_file, index=False)

        # 5. 统计
        total_time = time.time() - start_time
        self.logger.info("\n" + "="*80)
        self.logger.info("处理完成统计")
        self.logger.info("="*80)
        self.logger.info(f"总数: {total_rows}")
        self.logger.info(f"成功: {success_count}")
        self.logger.info(f"失败: {error_count}")
        self.logger.info(f"总耗时: {total_time/60:.1f}分钟")
        self.logger.info(f"平均: {total_time/total_rows:.1f}秒/条")

        return df

    def _process_parallel(self, df: pd.DataFrame, start_row: int, total_rows: int,
                          output_file: str, save_interval: int, max_workers: int):
        """并行处理模式"""

        success_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            future_to_index = {}
            for index in range(start_row, total_rows):
                row = df.iloc[index]
                row_data = self._extract_row_data(row)

                future = executor.submit(self._process_row_with_retry, row_data, index)
                future_to_index[future] = index

            # 收集结果
            completed = 0
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                completed += 1

                try:
                    result = future.result()

                    # 格式化并写入
                    formatted_result = self._format_result_for_excel(result)
                    for col, value in formatted_result.items():
                        df.at[index, col] = value

                    if result.get('score') is not None:
                        success_count += 1
                        self.logger.info(f"✓ [{completed}/{total_rows}] 行{index+1}: {result['score']}分")
                    else:
                        error_count += 1
                        self.logger.info(f"✗ [{completed}/{total_rows}] 行{index+1}: 失败")

                except Exception as e:
                    error_count += 1
                    self.logger.error(f"✗ [{completed}/{total_rows}] 行{index+1}: 异常 - {e}")
                    df.at[index, '状态'] = f"❌ 异常: {str(e)[:100]}"

                # 定期保存
                if completed % save_interval == 0 or completed == total_rows:
                    df.to_excel(output_file, index=False)
                    self.logger.info(f"💾 已保存进度: {completed}/{total_rows}")

    def generate_summary(self, df: pd.DataFrame) -> Dict:
        """生成汇总统计"""

        total = len(df)
        score_counts = {
            '0分': len(df[df['AI评分'] == 0]),
            '1分': len(df[df['AI评分'] == 1]),
            '2分': len(df[df['AI评分'] == 2]),
            '3分': len(df[df['AI评分'] == 3]),
            '失败': len(df[df['AI评分'].isna()])
        }

        summary = {
            '总数': total,
            **score_counts,
            '平均耗时': round(df['耗时(秒)'].mean(), 1) if '耗时(秒)' in df.columns else 0
        }

        self.logger.info("\n" + "="*80)
        self.logger.info("📊 评分统计")
        self.logger.info("="*80)
        for key, value in summary.items():
            if key == '总数' or key == '平均耗时':
                self.logger.info(f"{key}: {value}")
            else:
                percentage = (value / total * 100) if total > 0 else 0
                self.logger.info(f"{key}: {value} ({percentage:.1f}%)")

        return summary


# 命令行工具
def main():
    import argparse

    parser = argparse.ArgumentParser(description='Excel批量自动评分 V4')
    parser.add_argument('input_file', help='输入Excel文件')
    parser.add_argument('output_file', help='输出Excel文件')
    parser.add_argument('--config', default='config.json', help='配置文件')
    parser.add_argument('--start-row', type=int, default=0, help='开始行号（从0开始）')
    parser.add_argument('--save-interval', type=int, default=5, help='保存间隔')
    parser.add_argument('--parallel', action='store_true', help='启用并行处理')
    parser.add_argument('--max-workers', type=int, default=2, help='并行线程数（2-3推荐）')
    parser.add_argument('--no-search', action='store_true', help='禁用搜索验证（更快但不准确）')
    parser.add_argument('--log-level', default='INFO', help='日志级别')

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 创建处理器
    scorer = ExcelAutoScorer(args.config, enable_search=not args.no_search)

    # 处理Excel
    df = scorer.process_excel(
        input_file=args.input_file,
        output_file=args.output_file,
        save_interval=args.save_interval,
        start_row=args.start_row,
        parallel=args.parallel,
        max_workers=args.max_workers
    )

    # 生成统计
    summary = scorer.generate_summary(df)

    print("\n✅ 处理完成！")
    print(f"📁 结果已保存到: {args.output_file}")


if __name__ == "__main__":
    main()
