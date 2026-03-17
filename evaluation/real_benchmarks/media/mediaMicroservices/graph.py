import matplotlib.pyplot as plt

# 원래 데이터 (8분 결과)
minutes = [1, 2, 3, 4, 5, 6, 7, 8]

# 각 행의 원래 데이터
user_register_orig    = [4.410918, 2.725157, 2.402228, 2.207824, 1.971859, 1.391906, 1.064373, 0.00728824]
movie_register_orig   = [0.581383, 1.09191, 1.30857, 1.025275, 1.341865, 1.446398, 1.572494, 0.01152139]
review_compose_orig   = [1.54121, 2.575776, 2.697672, 2.855893, 2.816628, 2.909667, 2.902265, 0.02908407]
movie_info_write_orig = [0.525403, 0.045013, 0.377136, 0.312163, 0.875329, 0.361467, 0.632682, 0.00726489]
cast_info_write_orig  = [0, 0, 0.043969, 0.137539, 0, 0.379608, 0.263148, 0.00670993]
plot_write_orig       = [1.810418, 0.853316, 0.240942, 0.388299, 0.012172, 0.299818, 0.378216, 0.00623627]

# 1~7번째 값(인덱스 0~6)에 0.01을 곱하기
def adjust_data(data):
    return [val * 0.01 if idx < 7 else val for idx, val in enumerate(data)]

user_register    = adjust_data(user_register_orig)
movie_register   = adjust_data(movie_register_orig)
review_compose   = adjust_data(review_compose_orig)
movie_info_write = adjust_data(movie_info_write_orig)
cast_info_write  = adjust_data(cast_info_write_orig)
plot_write       = adjust_data(plot_write_orig)

# 그래프 그리기
plt.figure(figsize=(10, 6))
plt.plot(minutes, user_register, marker='o', label='user-register', color='red')
plt.plot(minutes, movie_register, marker='o', label='movie-register', color='blue')
plt.plot(minutes, review_compose, marker='o', label='review-compose', color='black')
plt.plot(minutes, movie_info_write, marker='o', label='movie-info-write', color='green')
plt.plot(minutes, cast_info_write, marker='o', label='cast-info-write',color='orange')
plt.plot(minutes, plot_write, marker='o', label='plot-write',color='purple')

plt.xlabel("Request Carbon Footprint Estimation Execution",fontsize=20)
plt.ylabel("Carbon Footprint (mg CO₂)",fontsize=18)
plt.ylim(-0.001,0.05)
plt.legend(fontsize=14)
plt.xticks(fontsize=14)
plt.yticks(fontsize=14)
plt.grid(True)
plt.tight_layout()
plt.show()
