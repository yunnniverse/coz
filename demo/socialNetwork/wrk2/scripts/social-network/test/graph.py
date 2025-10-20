import re
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np

def parse_log_from_file(file_path):
    """
    주어진 로그 파일에서 최종 svc-request 탄소 및 요청별 탄소 배출량을 추출하는 함수
    """
    with open(file_path, 'r', encoding='utf-8') as file:
        log_text = file.read()
        
    timestamps = []
    # service_data = {}  # 필요시 사용
    request_data = {}
    run_index = 1
    
    # 실행 단위 구분
    runs = log_text.strip().split("===========================================")
    
    for run in runs:
        if "최종 request 별 탄소" in run:
            timestamps.append(f"Run {run_index}")
            run_index += 1

            # 요청별 탄소 배출량 추출
            request_matches = re.findall(r"\*+최종 request 별 탄소\s*:\s*(\{.*\})\*+", run)
            if request_matches:
                # 내부 중괄호를 추출 (외부 키는 무시)
                inner_match = re.search(r"\{[^:]+:\s*\{(.*)\}\}", request_matches[0])
                if inner_match:
                    inner_text = inner_match.group(1)
                    # 예: "wrk2_api_post_compose: 0.791525815399994"
                    requests_found = re.findall(r"([-\w/]+):\s*([0-9.e-]+)", inner_text)
                    for req, value in requests_found:
                        if req not in request_data:
                            request_data[req] = [None] * (len(timestamps) - 1)  # 이전 실행 수만큼 None 채우기
                        request_data[req].append(float(value))
            # 누락된 요청 데이터 보정
            for request in request_data:
                while len(request_data[request]) < len(timestamps):
                    request_data[request].append(None)
    
    request_df = pd.DataFrame(request_data, index=timestamps)
    return request_df

def plot_graphs(request_df, oracle, output_path="carbon_emission_graph.png"):
    """
    요청별 탄소 배출량 변화를 그래프로 저장하는 함수.
    oracle 값에 해당하는 빨간색 실선을 추가합니다.
    """
    plt.figure(figsize=(12, 6))
    for request in request_df.columns:
        plt.plot(request_df.index, request_df[request], marker='o', linestyle='-', label=request)
    
    # oracle 값을 y축에 빨간 실선으로 그리기 (x축 전체에 걸쳐)
    plt.axhline(y=oracle, color='red', linestyle='-', linewidth=2, label=f'Oracle: {oracle}')
    
    plt.xticks(rotation=45)
    plt.xlabel("Run")
    plt.ylabel("Total Carbon Emission (kg CO2e)")
    plt.title("Total Request Carbon Emission Changes")
    plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"Graph saved at: {output_path}")

def compute_statistics(request_df):
    """
    요청별 분산, 표준편차, 변동률(CV)을 계산하여 DataFrame으로 반환하는 함수
    """
    df_stats = pd.DataFrame(columns=["Request", "Mean", "Variance", "StdDev", "CV(%)"])
    for request in request_df.columns:
        data = request_df[request].dropna()
        if len(data) == 0:
            continue
        mean_val = data.mean()
        var_val = data.var()
        std_val = data.std()
        cv_val = (std_val / mean_val) * 100 if mean_val != 0 else np.nan

        # DataFrame에 새 행 추가
        df_stats.loc[len(df_stats)] = {
            "Request": request,
            "Mean": mean_val,
            "Variance": var_val,
            "StdDev": std_val,
            "CV(%)": cv_val
        }
    return df_stats

# 사용 예시
file_path = "./9:1-1200/cic_result.txt"   # 로그 파일 경로
request_df = parse_log_from_file(file_path)

# 통계 계산 후 출력
stats_df = compute_statistics(request_df)
print("요청별 통계:")
print(stats_df)

# oracle 값 설정 (원하는 숫자 입력)
oracle = 0.081694729

# 그래프 저장 (oracle 값을 포함하여)
plot_graphs(request_df, oracle, output_path="carbon_emission_graph.png")
