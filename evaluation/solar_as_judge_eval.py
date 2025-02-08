import ast

from openai import OpenAI

from utils.configuration import Config
from utils.token_usage_counter import TokenUsageCounter
from utils.utils import retry_with_exponential_backoff


# TODO: 현재 사용되지 않는 함수(개선 및 반영 혹은, 삭제 필요)
@retry_with_exponential_backoff()
def run_solar_as_judge(
    source_texts: str,
    generated_texts: str,
    solar_as_judge_config: dict,
) -> dict:
    client = OpenAI(Config.user_upstage_api_key, base_url="https://api.upstage.ai/v1/solar")
    prompt_template_file_path = solar_as_judge_config.get("prompt_path", {})

    with open(prompt_template_file_path, "r", encoding="utf-8") as file:
        prompt_template = file.read()
    check_results = {}

    check_items = solar_as_judge_config["items"]  # {"item1": "...", "item2":"..."}
    items_one_volume = solar_as_judge_config["items_in_a_volume"]
    volumes_items = []

    for i in range(0, len(check_items), items_one_volume):
        volumes_items.append(list(check_items.values())[i : i + items_one_volume])

    for volume in volumes_items:
        check_items_str = ""
        count = 1
        for item in volume:
            check_items_str += f"{count}. {item}\n"
            count += 1

        prompt = prompt_template.format(
            source_text=source_texts, output_text=generated_texts, check_question=check_items_str
        )
        response = client.chat.completions.create(
            model="solar-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            seed=42,
        )
        TokenUsageCounter.add_usage("solar_as_judge", "solar_as_judge", response.usage.total_tokens)
        response_message = response.choices[0].message.content
        try:
            scores = ast.literal_eval(response_message)

            for i in range(len(volume)):
                check_item = volume[i]
                check_result = scores[i]
                check_results[check_item] = check_result

        except Exception as e:
            print(f"solar as judge 실행 중 오류가 발생했습니다: {e}")
            for i in range(len(volume)):
                check_item = volume[i]
                check_results[check_item] = 0

    return check_results
