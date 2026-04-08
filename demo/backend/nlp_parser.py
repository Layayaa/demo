"""
自然语言查询解析模块
工程造价材料查询 - 专业解析方案

架构：
用户输入 → 预处理 → 分词 → 意图识别 → 实体识别 → 参数校验 → 返回结构化参数
"""

import re
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple, Any


class NLPParser:
    """自然语言查询解析器"""

    def __init__(self, db_session=None):
        """
        初始化解析器

        Args:
            db_session: SQLAlchemy session，用于加载材料词库
        """
        self.db_session = db_session
        self.material_names = []  # 材料名称词库
        self.region_names = []    # 地区名称词库

        # 尝试加载jieba
        self.jieba_available = False
        try:
            import jieba
            import jieba.posseg as pseg
            self.jieba = jieba
            self.pseg = pseg
            self.jieba_available = True
            # 设置jieba不输出日志
            self.jieba.setLogLevel(20)
        except ImportError:
            pass

        # 尝试加载cpca
        self.cpca_available = False
        try:
            import cpca
            self.cpca = cpca
            self.cpca_available = True
        except ImportError:
            pass

        # 尝试加载dateparser
        self.dateparser_available = False
        try:
            import dateparser
            self.dateparser = dateparser
            self.dateparser_available = True
        except ImportError:
            pass

        # 初始化词库
        self._init_keywords()

    def _init_keywords(self):
        """初始化关键词库"""
        # 材料名称关键词（常用材料）
        self.material_keywords = [
            # 混凝土类
            '混凝土', '商品混凝土', '预拌混凝土', '商砼', '砼',
            # 钢材类
            '钢筋', '螺纹钢', '钢材', '钢板', '钢管', '型钢', '角钢', '槽钢', '工字钢',
            # 水泥类
            '水泥', '硅酸盐水泥', '普通水泥',
            # 砂石类
            '砂', '砂子', '石子', '碎石', '砂石', '河砂', '机制砂', '骨料',
            # 砖类
            '砖', '红砖', '青砖', '页岩砖', '空心砖', '加气砖', '砌块',
            # 门窗类
            '门', '窗', '木门', '防盗门', '防火门', '铝合金门窗', '塑钢门窗',
            # 管材类
            '管', '管材', '管道', '水管', '钢管', 'PVC管', 'PE管', '铸铁管',
            # 涂料类
            '涂料', '油漆', '乳胶漆', '墙面漆', '外墙涂料', '内墙涂料',
            # 防水材料
            '防水材料', '防水卷材', '防水涂料', '防水砂浆',
            # 保温材料
            '保温材料', '保温板', '保温棉', '隔热材料',
            # 沥青类
            '沥青', '柏油', '沥青混凝土',
            # 电缆类
            '电缆', '电线', '电力电缆', '控制电缆',
            # 灯具类
            '灯具', '灯', '照明设备', 'LED灯',
            # 模板脚手架
            '模板', '脚手架', '木模板', '钢模板',
            # 管桩
            '管桩', '预应力管桩', 'PHC管桩',
            # 玻璃
            '玻璃', '钢化玻璃', '中空玻璃',
            # 其他
            '石材', '瓷砖', '地板', '涂料', '胶', '密封胶',
        ]

        # 地区名称（四川主要城市）
        self.region_keywords = [
            '成都', '绵阳', '德阳', '宜宾', '泸州', '乐山', '南充', '自贡',
            '达州', '广元', '遂宁', '内江', '资阳', '眉山', '广安', '雅安',
            '巴中', '攀枝花', '凉山', '阿坝', '甘孜', '天府新区',
            '四川',
        ]

        # 规格型号正则规则（按优先级排序）
        self.spec_patterns = [
            # 混凝土强度等级
            (r'([CPOF]\d+(?:\.\d+)?)', '混凝土强度'),
            # 直径表示
            (r'([Φφϕ]\d+(?:\.\d+)?)', '直径'),
            (r'(D\d+(?:\.\d+)?)', '直径D'),
            # 尺寸表示
            (r'(\d+(?:\.\d+)?\s*[×x*×X]\s*\d+(?:\.\d+)?(?:\s*[×x*×X]\s*\d+(?:\.\d+)?)?)', '尺寸'),
            # 带单位
            (r'(\d+(?:\.\d+)?\s*mm²?)', '毫米'),
            (r'(\d+(?:\.\d+)?\s*cm²?)', '厘米'),
            (r'(\d+(?:\.\d+)?\s*m\b)', '米'),
            (r'(\d+(?:\.\d+)?\s*kg)', '千克'),
            (r'(\d+(?:\.\d+)?\s*吨)', '吨'),
            (r'(\d+(?:\.\d+)?\s*方)', '方'),
            (r'(\d+(?:\.\d+)?\s*W)', '瓦特'),
            # 纯数字规格
            (r'(\d+(?:\.\d+)?号)', '号数'),
            (r'(\d+(?:\.\d+)?级)', '等级'),
            (r'(\d+芯)', '芯数'),
            # 钢筋规格
            (r'(HRB\d+)', '钢筋牌号'),
            (r'(HPB\d+)', '钢筋牌号'),
        ]

        # 时间关键词映射
        self.time_keywords = {
            # 相对日期 - 单日
            '今天': ('day', 0),
            '昨日': ('day', -1),
            '昨天': ('day', -1),
            '前天': ('day', -2),
            '明天': ('day', 1),

            # 相对日期 - 范围
            '本周': ('week', 0),
            '这周': ('week', 0),
            '上周': ('week', -1),
            '下周': ('week', 1),
            '本月': ('month', 0),
            '这个月': ('month', 0),
            '上个月': ('month', -1),
            '上月': ('month', -1),
            '下个月': ('month', 1),
            '今年': ('year', 0),
            '去年': ('year', -1),
            '明年': ('year', 1),
        }

        # 意图关键词
        self.intent_keywords = {
            'comparison': ['比较', '对比', '比价', '哪个便宜', '哪个贵'],
            'trend': ['趋势', '走势', '变化', '涨跌', '涨了', '跌了', '波动'],
            'statistics': ['平均', '最高', '最低', '统计', '汇总'],
            'price_inquiry': ['价格', '多少钱', '报价', '询价', '单价', '费用'],
        }

    def load_material_names(self):
        """从数据库加载材料名称词库"""
        if self.db_session is None:
            return

        try:
            from models import PriceRecord
            # 获取所有不重复的材料名称
            results = self.db_session.query(PriceRecord.material_name).distinct().all()
            self.material_names = [r[0] for r in results if r[0]]

            # 添加到jieba自定义词典
            if self.jieba_available:
                for name in self.material_names:
                    if len(name) >= 2:
                        self.jieba.add_word(name, freq=1000, tag='nz')  # nz=其他专名

            print(f"[NLP] 已加载 {len(self.material_names)} 个材料名称")
        except Exception as e:
            print(f"[NLP] 加载材料名称失败: {e}")

    # ==================== 预处理模块 ====================

    def preprocess(self, text: str) -> str:
        """
        预处理用户输入

        - 去除首尾空格
        - 全角转半角
        - 标点符号统一
        """
        if not text:
            return ""

        # 去除首尾空格
        text = text.strip()

        # 全角转半角
        rstring = ""
        for char in text:
            inside_code = ord(char)
            if inside_code == 12288:  # 全角空格
                inside_code = 32
            elif 65281 <= inside_code <= 65374:  # 全角字符
                inside_code -= 65248
            rstring += chr(inside_code)
        text = rstring

        # 去除常见语气词前缀
        prefixes = ['查询', '查找', '搜索', '查看', '帮我查', '帮忙查', '请查', '请问', '查一下', '查下']
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break

        return text.strip()

    # ==================== 分词模块 ====================

    def tokenize(self, text: str) -> List[str]:
        """
        分词

        Args:
            text: 预处理后的文本

        Returns:
            分词列表
        """
        if self.jieba_available:
            # 使用jieba精确模式分词
            return list(self.jieba.cut(text, cut_all=False))
        else:
            # 降级：简单的中文分词（按字符和数字/英文分组）
            tokens = []
            # 提取中文词组（2-4字）
            chinese_pattern = r'[\u4e00-\u9fa5]{2,4}'
            chinese_matches = re.findall(chinese_pattern, text)
            tokens.extend(chinese_matches)
            # 提取英文+数字组合（如C30）
            alnum_pattern = r'[A-Za-z]+\d+(?:\.\d+)?'
            alnum_matches = re.findall(alnum_pattern, text)
            tokens.extend(alnum_matches)
            # 提取数字+单位
            num_pattern = r'\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|吨|方|号|级|芯|W|x|×)'
            num_matches = re.findall(num_pattern, text)
            tokens.extend(num_matches)

            return tokens

    # ==================== 意图识别模块 ====================

    def detect_intent(self, text: str) -> str:
        """
        识别查询意图

        Returns:
            intent: 'comparison' | 'trend' | 'statistics' | 'price_inquiry'
        """
        for intent, keywords in self.intent_keywords.items():
            for kw in keywords:
                if kw in text:
                    return intent
        return 'price_inquiry'  # 默认询价

    # ==================== 实体识别模块 ====================

    def extract_material(self, text: str, tokens: List[str]) -> Tuple[Optional[str], List[str]]:
        """
        提取材料名称

        Returns:
            material_name: 识别到的材料名称
            candidates: 候选列表（用于歧义处理）
        """
        candidates = []

        # 策略1: 从预定义关键词匹配
        for keyword in self.material_keywords:
            if keyword in text:
                candidates.append(keyword)

        # 策略2: 从数据库加载的材料名称匹配
        for name in self.material_names:
            if name in text and name not in candidates:
                candidates.append(name)

        # 策略3: 从分词结果中匹配
        for token in tokens:
            if token in self.material_keywords and token not in candidates:
                candidates.append(token)

        # 按长度排序（优先选择更精确的匹配）
        candidates.sort(key=len, reverse=True)

        # 返回最匹配的
        if candidates:
            return candidates[0], candidates

        # 策略4: 降级 - 提取中文词组作为材料名
        chinese_words = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
        # 过滤掉地区词
        chinese_words = [w for w in chinese_words if w not in self.region_keywords]
        if chinese_words:
            return chinese_words[0], chinese_words

        return None, []

    def extract_specification(self, text: str, tokens: List[str]) -> Optional[str]:
        """
        提取规格型号
        """
        # 按优先级尝试每个正则规则
        for pattern, desc in self.spec_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                spec = match.group(1).upper()
                # 清理空格
                spec = re.sub(r'\s+', '', spec)
                return spec

        return None

    def extract_region(self, text: str, tokens: List[str]) -> Optional[str]:
        """
        提取地区
        """
        # 策略1: 使用cpca库
        if self.cpca_available:
            try:
                result = self.cpca.transform([text])
                if result is not None and len(result) > 0:
                    row = result.iloc[0]
                    # 组合省市区
                    parts = []
                    if row['省'] and row['省'] != '':
                        parts.append(row['省'])
                    if row['市'] and row['市'] != '':
                        parts.append(row['市'])
                    if row['区'] and row['区'] != '':
                        parts.append(row['区'])
                    if parts:
                        return ''.join(parts)
            except Exception as e:
                pass

        # 策略2: 关键词匹配
        for region in self.region_keywords:
            if region in text:
                return region

        # 策略3: 正则匹配
        patterns = [
            r'(\w+市)', r'(\w+省)', r'(\w+区)', r'(\w+县)'
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)

        return None

    def extract_time_range(self, text: str) -> Tuple[Optional[date], Optional[date], Optional[str]]:
        """
        提取时间范围

        Returns:
            start_date: 开始日期
            end_date: 结束日期
            display: 显示文本
        """
        today = date.today()

        # 中文数字转换
        chinese_to_num = {
            '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            '两': 2, '几': 3  # "两"个月、"几"个月
        }

        def convert_chinese_num(s: str) -> int:
            """将中文数字或阿拉伯数字转为int"""
            s = s.strip()
            if s.isdigit():
                return int(s)
            if s in chinese_to_num:
                return chinese_to_num[s]
            return 1  # 默认

        # 策略1: 关键词映射
        for keyword, (unit, offset) in self.time_keywords.items():
            if keyword in text:
                if unit == 'day':
                    target = today + timedelta(days=offset)
                    return target, target, keyword
                elif unit == 'week':
                    # 计算周的起始
                    weekday = today.weekday()
                    start = today - timedelta(days=weekday) + timedelta(weeks=offset)
                    end = start + timedelta(days=6)
                    return start, end, keyword
                elif unit == 'month':
                    # 计算月的起始
                    if offset == 0:
                        start = today.replace(day=1)
                    elif offset < 0:
                        # 上N个月
                        month = today.month + offset
                        year = today.year
                        while month <= 0:
                            month += 12
                            year -= 1
                        start = date(year, month, 1)
                    else:
                        month = today.month + offset
                        year = today.year
                        while month > 12:
                            month -= 12
                            year += 1
                        start = date(year, month, 1)

                    # 计算月末
                    if start.month == 12:
                        end = date(start.year + 1, 1, 1) - timedelta(days=1)
                    else:
                        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
                    return start, end, keyword
                elif unit == 'year':
                    year = today.year + offset
                    start = date(year, 1, 1)
                    end = date(year, 12, 31)
                    return start, end, keyword

        # 策略2: 正则匹配时间范围表达式
        # 最近N天/月/年（支持中文数字）
        match = re.search(r'(?:最近|近)([一二三四五六七八九十两几\d]+)(天|个月|月|年)', text)
        if match:
            num = convert_chinese_num(match.group(1))
            unit = match.group(2)
            end = today
            if unit == '天':
                start = today - timedelta(days=num)
            elif unit in ['个月', '月']:
                # 简化：按30天计算
                start = today - timedelta(days=num * 30)
            elif unit == '年':
                start = today - timedelta(days=num * 365)
            return start, end, f'最近{num}{unit}'

        # N月到M月
        match = re.search(r'(\d+)月到(\d+)月', text)
        if match:
            m1, m2 = int(match.group(1)), int(match.group(2))
            start = date(today.year, m1, 1)
            if m2 == 12:
                end = date(today.year, 12, 31)
            else:
                end = date(today.year, m2 + 1, 1) - timedelta(days=1)
            return start, end, f'{m1}月到{m2}月'

        # N年到M年
        match = re.search(r'(\d{4})年到(\d{4})年', text)
        if match:
            y1, y2 = int(match.group(1)), int(match.group(2))
            start = date(y1, 1, 1)
            end = date(y2, 12, 31)
            return start, end, f'{y1}年到{y2}年'

        # 策略3: 使用dateparser解析具体日期
        if self.dateparser_available:
            try:
                # 尝试解析日期
                parsed = self.dateparser.parse(text, languages=['zh'])
                if parsed:
                    return parsed.date(), parsed.date(), parsed.strftime('%Y-%m-%d')
            except:
                pass

        # 策略4: 标准日期格式
        date_patterns = [
            (r'(\d{4}-\d{1,2}-\d{1,2})', '%Y-%m-%d'),
            (r'(\d{4}/\d{1,2}/\d{1,2})', '%Y/%m/%d'),
            (r'(\d{4}年\d{1,2}月\d{1,2}日)', '%Y年%m月%d日'),
            (r'(\d{1,2}月\d{1,2}日)', None),  # 需要特殊处理
        ]

        for pattern, fmt in date_patterns:
            match = re.search(pattern, text)
            if match:
                date_str = match.group(1)
                try:
                    if fmt:
                        parsed = datetime.strptime(date_str, fmt).date()
                    else:
                        # 只有月日，默认今年
                        m = re.search(r'(\d{1,2})月(\d{1,2})日', date_str)
                        if m:
                            parsed = date(today.year, int(m.group(1)), int(m.group(2)))
                        else:
                            continue
                    return parsed, parsed, date_str
                except:
                    continue

        return None, None, None

    # ==================== 参数校验模块 ====================

    def validate_params(self, params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        校验参数

        Returns:
            is_valid: 是否有效
            errors: 错误信息列表
        """
        errors = []

        # 必填项检查
        if not params.get('material_name'):
            errors.append('未识别到材料名称')

        # 日期合法性
        start = params.get('start_date')
        end = params.get('end_date')
        if start and end and start > end:
            # 自动调换
            params['start_date'], params['end_date'] = end, start

        return len(errors) == 0, errors

    # ==================== 主解析入口 ====================

    def parse(self, text: str) -> Dict[str, Any]:
        """
        解析自然语言查询

        Args:
            text: 用户输入的查询文本

        Returns:
            结构化参数字典
        """
        # 1. 预处理
        cleaned_text = self.preprocess(text)

        # 2. 分词
        tokens = self.tokenize(cleaned_text)

        # 3. 意图识别
        intent = self.detect_intent(cleaned_text)

        # 4. 实体识别
        material_name, material_candidates = self.extract_material(cleaned_text, tokens)
        specification = self.extract_specification(cleaned_text, tokens)
        region = self.extract_region(cleaned_text, tokens)
        start_date, end_date, time_display = self.extract_time_range(cleaned_text)

        # 5. 构建结果
        result = {
            'raw_query': text,
            'cleaned_query': cleaned_text,
            'tokens': tokens,
            'parsed_intent': intent,
            'material_name': material_name,
            'material_candidates': material_candidates,
            'specification': specification,
            'region': region,
            'start_date': start_date,
            'end_date': end_date,
            'time_display': time_display,
            'price': None,  # 暂不支持
            'unit': None,   # 暂不支持
        }

        # 6. 参数校验
        is_valid, errors = self.validate_params(result)
        result['is_valid'] = is_valid
        result['errors'] = errors

        # 7. 设置默认值
        if not start_date:
            # 默认近1年
            result['start_date'] = date.today() - timedelta(days=365)
            result['end_date'] = date.today()
            result['time_display'] = '近1年（默认）'

        return result

    def to_query_params(self, parse_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        将解析结果转换为数据库查询参数

        Returns:
            适用于数据库查询的参数字典
        """
        return {
            'material_name': parse_result.get('material_name'),
            'material_candidates': parse_result.get('material_candidates', []),
            'specification': parse_result.get('specification'),
            'region': parse_result.get('region'),
            'start_date': parse_result.get('start_date'),
            'end_date': parse_result.get('end_date'),
            'intent': parse_result.get('parsed_intent'),
        }


# ==================== 便捷函数 ====================

_parser_instance = None

def get_parser(db_session=None) -> NLPParser:
    """获取解析器单例"""
    global _parser_instance
    if _parser_instance is None:
        _parser_instance = NLPParser(db_session)
        if db_session:
            _parser_instance.load_material_names()
    return _parser_instance

def parse_query(text: str, db_session=None) -> Dict[str, Any]:
    """
    解析自然语言查询（便捷函数）

    Args:
        text: 用户输入
        db_session: 数据库session（可选）

    Returns:
        解析结果字典
    """
    parser = get_parser(db_session)
    return parser.parse(text)


# ==================== 测试代码 ====================

if __name__ == '__main__':
    # 测试
    parser = NLPParser()

    test_cases = [
        '成都最近一个月钢筋C30的价格',
        '门的价格',
        '德阳商品混凝土',
        '最近7天水泥价格',
        '上个月钢材趋势',
        'C30混凝土和C40混凝土对比',
    ]

    for case in test_cases:
        print(f"\n输入: {case}")
        result = parser.parse(case)
        print(f"意图: {result['parsed_intent']}")
        print(f"材料: {result['material_name']}")
        print(f"规格: {result['specification']}")
        print(f"地区: {result['region']}")
        print(f"时间: {result['time_display']}")
        print(f"候选: {result['material_candidates']}")
