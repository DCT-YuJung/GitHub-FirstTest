import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_customer_data(url):
    """
    從網頁中提取客戶資料
    """
    try:
        # 發送請求
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            print(f"無法訪問網頁，狀態碼: {response.status_code}")
            return []
        
        # 解析HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 移除script和style標籤
        for script in soup(["script", "style"]):
            script.decompose()
        
        # 獲取網頁文字內容
        text = soup.get_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        customers = []
        
        # 電話號碼的正則表達式（台灣格式）
        phone_pattern = r'(?:(?:0[2-9][\-\s]?)?[0-9]{3,4}[\-\s]?[0-9]{4}|09[0-9]{2}[\-\s]?[0-9]{3}[\-\s]?[0-9]{3})'
        
        # 地址關鍵字
        address_keywords = ['市', '區', '路', '街', '巷', '弄', '號', '縣', '鄉', '鎮', '村']
        
        # 嘗試找出包含客戶資訊的區塊
        for i, line in enumerate(lines):
            # 尋找電話號碼
            phone_match = re.search(phone_pattern, line)
            
            if phone_match:
                customer = {
                    'name': '',
                    'phone': phone_match.group(),
                    'address': '',
                    'other': []
                }
                
                # 往前找名稱（通常在電話前面1-3行）
                for j in range(max(0, i-3), i):
                    if len(lines[j]) >= 2 and len(lines[j]) <= 20:
                        # 排除純數字或包含特殊符號的行
                        if not lines[j].isdigit() and not re.search(r'[|@#$%^&*()]', lines[j]):
                            customer['name'] = lines[j]
                            break
                
                # 往後找地址（通常在電話後面1-3行）
                for j in range(i+1, min(len(lines), i+4)):
                    if any(keyword in lines[j] for keyword in address_keywords):
                        customer['address'] = lines[j]
                        break
                
                # 收集附近其他可能相關的資訊
                for j in range(max(0, i-2), min(len(lines), i+3)):
                    if j != i and lines[j] not in [customer['name'], customer['address']]:
                        if len(lines[j]) > 0 and not re.search(phone_pattern, lines[j]):
                            customer['other'].append(lines[j])
                
                customers.append(customer)
        
        return customers
        
    except Exception as e:
        print(f"發生錯誤: {str(e)}")
        return []

def save_to_file(customers, filename=None):
    """
    將客戶資料儲存到txt檔案
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"客戶資料_{timestamp}.txt"
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write(f"客戶資料清單 - 擷取時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")
        
        for idx, customer in enumerate(customers, 1):
            f.write(f"【客戶 {idx}】\n")
            f.write(f"名稱: {customer['name']}\n")
            f.write(f"電話: {customer['phone']}\n")
            f.write(f"地址: {customer['address']}\n")
            
            if customer['other']:
                f.write("其他資訊:\n")
                for info in customer['other'][:5]:  # 最多顯示5條其他資訊
                    f.write(f"  - {info}\n")
            
            f.write("-" * 60 + "\n\n")
        
        f.write(f"\n總共找到 {len(customers)} 筆客戶資料\n")
    
    print(f"資料已儲存至: {filename}")
    return filename

# 主程式
if __name__ == "__main__":
    print("=" * 60)
    print("網頁客戶資料擷取程式")
    print("=" * 60)
    
    url = input("\n請輸入網址: ").strip()
    
    if not url:
        print("未輸入網址，程式結束")
        exit()
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    print(f"\n正在擷取資料從: {url}")
    print("請稍候...\n")
    
    customers = extract_customer_data(url)
    
    if customers:
        print(f"成功找到 {len(customers)} 筆客戶資料\n")
        
        # 預覽前3筆
        print("前3筆資料預覽:")
        print("-" * 60)
        for i, customer in enumerate(customers[:3], 1):
            print(f"客戶 {i}:")
            print(f"  名稱: {customer['name']}")
            print(f"  電話: {customer['phone']}")
            print(f"  地址: {customer['address']}")
            print()
        
        # 儲存檔案
        filename = save_to_file(customers)
        print(f"\n✓ 完成！共擷取 {len(customers)} 筆資料")
    else:
        print("未找到客戶資料，請確認網址是否正確或網頁格式")