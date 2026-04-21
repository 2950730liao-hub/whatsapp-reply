"""
报价辅助服务 - 材料清单和价格计算
"""
import sqlite3
import json
from typing import List, Dict, Optional
from datetime import datetime
from decimal import Decimal


class QuotationService:
    """报价服务"""
    
    def __init__(self, db_path: str = "./data/quotations.db"):
        self.db_path = db_path
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 材料表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                unit TEXT NOT NULL,
                unit_price REAL NOT NULL,
                category TEXT DEFAULT 'general',
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 报价单表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER,
                title TEXT NOT NULL,
                items TEXT NOT NULL,
                total_amount TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        
        # 添加默认材料
        self._add_default_materials()
    
    def _add_default_materials(self):
        """添加默认材料"""
        defaults = [
            ("钢材", "吨", 5000, "原材料", "普通碳钢"),
            ("铝材", "吨", 18000, "原材料", "6061铝合金"),
            ("铜材", "吨", 65000, "原材料", "T2紫铜"),
            ("螺丝", "个", 0.5, "五金", "M6不锈钢螺丝"),
            ("螺母", "个", 0.3, "五金", "M6不锈钢螺母"),
            ("垫片", "个", 0.1, "五金", "M6不锈钢垫片"),
            ("电线", "米", 2.5, "电气", "BV2.5平方电线"),
            ("电缆", "米", 15, "电气", "YJV3*4电缆"),
            ("人工费", "小时", 80, "人工", "技术工人"),
            ("运输费", "次", 200, "物流", "市内配送"),
        ]
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for name, unit, price, category, desc in defaults:
            cursor.execute("""
                INSERT OR IGNORE INTO materials (name, unit, unit_price, category, description)
                VALUES (?, ?, ?, ?, ?)
            """, (name, unit, price, category, desc))
        
        conn.commit()
        conn.close()
    
    def get_materials(self, category: Optional[str] = None) -> List[dict]:
        """获取材料列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if category:
            cursor.execute("""
                SELECT id, name, unit, unit_price, category, description
                FROM materials WHERE category = ?
                ORDER BY category, name
            """, (category,))
        else:
            cursor.execute("""
                SELECT id, name, unit, unit_price, category, description
                FROM materials ORDER BY category, name
            """)
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "name": row[1],
                "unit": row[2],
                "unit_price": row[3],
                "category": row[4],
                "description": row[5]
            })
        
        conn.close()
        return results
    
    def add_material(self, name: str, unit: str, unit_price: float,
                    category: str = "general", description: str = "") -> int:
        """添加材料"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO materials (name, unit, unit_price, category, description)
            VALUES (?, ?, ?, ?, ?)
        """, (name, unit, unit_price, category, description))
        
        material_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return material_id
    
    def create_quotation(self, customer_id: int, title: str, 
                        items: List[dict], notes: str = "") -> dict:
        """
        创建报价单
        
        Args:
            items: [{"material_id": 1, "quantity": 10, "remark": ""}]
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 计算总价
        total = Decimal('0')
        detailed_items = []
        
        for item in items:
            cursor.execute("""
                SELECT id, name, unit, unit_price FROM materials WHERE id = ?
            """, (item["material_id"],))
            
            row = cursor.fetchone()
            if row:
                unit_price = Decimal(str(row[3]))
                quantity = Decimal(str(item["quantity"]))
                subtotal = (unit_price * quantity).quantize(Decimal('0.01'))
                total += subtotal
                
                detailed_items.append({
                    "material_id": row[0],
                    "name": row[1],
                    "unit": row[2],
                    "unit_price": float(unit_price),
                    "quantity": float(quantity),
                    "subtotal": float(subtotal),
                    "remark": item.get("remark", "")
                })
        
        # 保存报价单 - 使用Decimal确保精度
        total_amount_str = str(total.quantize(Decimal('0.01')))
        cursor.execute("""
            INSERT INTO quotations (customer_id, title, items, total_amount, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (customer_id, title, json.dumps(detailed_items), total_amount_str, notes))
        
        quotation_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return {
            "id": quotation_id,
            "customer_id": customer_id,
            "title": title,
            "items": detailed_items,
            "total_amount": total,
            "notes": notes
        }
    
    def get_quotations(self, customer_id: Optional[int] = None) -> List[dict]:
        """获取报价单列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if customer_id:
            cursor.execute("""
                SELECT id, customer_id, title, total_amount, status, created_at
                FROM quotations WHERE customer_id = ?
                ORDER BY created_at DESC
            """, (customer_id,))
        else:
            cursor.execute("""
                SELECT id, customer_id, title, total_amount, status, created_at
                FROM quotations ORDER BY created_at DESC
            """)
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "customer_id": row[1],
                "title": row[2],
                "total_amount": Decimal(row[3]),
                "status": row[4],
                "created_at": row[5]
            })
        
        conn.close()
        return results
    
    def get_quotation(self, quotation_id: int) -> Optional[dict]:
        """获取报价单详情"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, customer_id, title, items, total_amount, status, notes, created_at
            FROM quotations WHERE id = ?
        """, (quotation_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row[0],
                "customer_id": row[1],
                "title": row[2],
                "items": json.loads(row[3]),
                "total_amount": Decimal(row[4]),
                "status": row[5],
                "notes": row[6],
                "created_at": row[7]
            }
        return None
    
    def format_quotation_text(self, quotation: dict) -> str:
        """格式化报价单为文本"""
        text = f"📋 {quotation['title']}\n"
        text += "=" * 30 + "\n\n"
        
        for i, item in enumerate(quotation['items'], 1):
            text += f"{i}. {item['name']}\n"
            text += f"   数量: {item['quantity']} {item['unit']}\n"
            text += f"   单价: ¥{item['unit_price']:.2f}\n"
            text += f"   小计: ¥{item['subtotal']:.2f}\n"
            if item.get('remark'):
                text += f"   备注: {item['remark']}\n"
            text += "\n"
        
        text += "=" * 30 + "\n"
        total_amount = quotation['total_amount']
        if isinstance(total_amount, Decimal):
            text += f"💰 总计: ¥{total_amount:.2f}\n"
        else:
            text += f"💰 总计: ¥{float(total_amount):.2f}\n"
        
        if quotation.get('notes'):
            text += f"\n📝 备注: {quotation['notes']}\n"
        
        return text


# 全局实例
_quotation_service: Optional[QuotationService] = None


def get_quotation_service() -> QuotationService:
    """获取报价服务实例"""
    global _quotation_service
    if _quotation_service is None:
        _quotation_service = QuotationService()
    return _quotation_service
