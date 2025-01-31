import argparse
import json

import torch
import yaml
from bert_score import score as bert_score
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from openai import OpenAI
from rouge_score import rouge_scorer

load_dotenv()
client = OpenAI()


def load_config(config_path):
    """
    주어진 경로의 YAML 파일을 로드해 dict 형태로 반환
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def load_data_json(json_file):
    """
    JSON을 로드하여 source, summarized, gold 리스트를 반환
    """
    with open(json_file, "r", encoding="utf-8") as f:
        data_list = json.load(f)  # [{...}, {...}, ...] 형태

    source_texts = []
    summarized_texts = []
    gold_texts = []

    for item in data_list:
        source_texts.append(item["source"])
        summarized_texts.append(item["system_output"])
        gold_texts.append(item["reference"])

    return source_texts, summarized_texts, gold_texts


def calculate_rouge(gold_texts, generated_texts):
    """
    gold_texts, generated_texts: 길이 N 리스트
    각 쌍에 대해 ROUGE-1, ROUGE-2, ROUGE-L 계산
    return 형태 : [{"rouge1":(p,r,f), "rouge2":(p,r,f), "rougeL":(p,r,f)}, ...]
    """
    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)
    results = []
    for gold, gen in zip(gold_texts, generated_texts):
        scores = scorer.score(gold, gen)
        item = {
            "rouge1": (
                scores["rouge1"].precision,
                scores["rouge1"].recall,
                scores["rouge1"].fmeasure,
            ),
            "rouge2": (
                scores["rouge2"].precision,
                scores["rouge2"].recall,
                scores["rouge2"].fmeasure,
            ),
            "rougeL": (
                scores["rougeL"].precision,
                scores["rougeL"].recall,
                scores["rougeL"].fmeasure,
            ),
        }
        results.append(item)
    return results


def calculate_bert(gold_texts, generated_texts, model_type="distilbert-base-uncased"):
    """
    BERTScore를 한 번에 계산 → 각 샘플별 (p, r, f) 튜플 리스트로 반환
    """
    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    P, R, F1 = bert_score(cands=generated_texts, refs=gold_texts, model_type=model_type, device=device)
    results = []
    for i in range(len(gold_texts)):
        results.append((P[i].item(), R[i].item(), F1[i].item()))
    return results


def calculate_g_eval(source_texts, generated_texts, config):
    """
    4가지 관점(consistency, coherence, fluency, relevance)에 대해
    각 source+summary 쌍마다 OpenAI API를 4번 호출하고, 결과 점수를 반환.
    반환 형태:
      [
        {"consistency": float, "coherence": float, "fluency": float, "relevance": float},
        {"consistency": float, "coherence": float, "fluency": float, "relevance": float},
        ...
      ]
    """
    # g_eval 세팅
    g_eval_config = config.get("g_eval", {})
    prompt_files = g_eval_config.get("prompts", {})
    model_name = g_eval_config.get("openai_model", "gpt-4")

    # 평가할 네 가지 aspects
    aspects = ["consistency", "coherence", "fluency", "relevance"]

    results_list = []
    for src, gen in zip(source_texts, generated_texts):
        aspect_scores = {}
        for aspect in aspects:
            prompt_path = prompt_files.get(aspect, None)
            if not prompt_path:
                # 프롬프트 파일이 없다면 0으로 처리
                aspect_scores[aspect] = 0.0
                continue

            # 프롬프트 읽어오기
            with open(prompt_path, "r", encoding="utf-8") as f:
                base_prompt = f.read()

            # {{Document}}, {{Summary}} 치환
            cur_prompt = base_prompt.format(Document=src, Summary=gen)

            # OpenAI API 호출
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "system", "content": cur_prompt}],
                    temperature=0.7,
                    max_tokens=50,
                    n=1,
                )
                # GPT가 준 output을 float로 파싱
                gpt_text = response.choices[0].message.content.strip()
                score_value = float(gpt_text)
                aspect_scores[aspect] = score_value
            except Exception as e:
                print(f"[Error in G-EVAL] aspect={aspect}, error={e}")
                aspect_scores[aspect] = 0.0

        results_list.append(aspect_scores)

    return results_list


def validate_data_lengths(metrics, source_texts, summarized_texts, gold_texts):
    """
    메트릭 종류에 따라 데이터 길이(source, gold 등)를 간단히 체크
    """
    n_summaries = len(summarized_texts)

    if "rouge" in metrics or "bert" in metrics:
        if len(gold_texts) != n_summaries:
            raise ValueError("JSON 내 gold 열과 summarized 열의 줄 수가 다릅니다.")
    if "g-eval" in metrics:
        if len(source_texts) != n_summaries:
            raise ValueError("JSON 내 source 열과 summarized 열의 줄 수가 다릅니다.")


def compute_metrics(config, source_texts, summarized_texts, gold_texts):
    """
    설정(config)에 따라 ROUGE / BERT / G-EVAL 계산
    """
    metrics = config.get("metrics", [])
    bert_model_type = config.get("bert_model", "distilbert-base-uncased")
    results = {}

    if "rouge" in metrics:
        rouge_res = calculate_rouge(gold_texts, summarized_texts)
        results["rouge"] = rouge_res

    if "bert" in metrics:
        bert_res = calculate_bert(gold_texts, summarized_texts, model_type=bert_model_type)
        results["bert"] = bert_res

    if "g-eval" in metrics:
        geval_res = calculate_g_eval(source_texts, summarized_texts, config)
        results["g-eval"] = geval_res

    return results


def print_per_item_scores(results, source_texts, summarized_texts, gold_texts):
    """
    각 샘플별 스코어를 출력
    """
    n_items = len(summarized_texts)
    for i in range(n_items):
        print(f"\n--- Sample {i+1} ---")

        # ROUGE
        if "rouge" in results:
            ritem = results["rouge"][i]
            r1p, r1r, r1f = ritem["rouge1"]
            r2p, r2r, r2f = ritem["rouge2"]
            rlp, rlr, rlf = ritem["rougeL"]
            print(
                f"[ROUGE] R1=(P:{r1p:.4f},R:{r1r:.4f},F:{r1f:.4f}), "
                f"R2=(P:{r2p:.4f},R:{r2r:.4f},F:{r2f:.4f}), "
                f"RL=(P:{rlp:.4f},R:{rlr:.4f},F:{rlf:.4f})"
            )

        # BERT
        if "bert" in results:
            bp, br, bf = results["bert"][i]
            print(f"[BERT] P:{bp:.4f}, R:{br:.4f}, F:{bf:.4f}")

        # G-EVAL
        if "g-eval" in results:
            gitem = results["g-eval"][i]
            con = gitem["consistency"]
            coh = gitem["coherence"]
            flu = gitem["fluency"]
            rel = gitem["relevance"]
            print("[G-EVAL] " f"consistency={con:.4f}, coherence={coh:.4f}, " f"fluency={flu:.4f}, relevance={rel:.4f}")


def print_averages(results, n_items):
    """
    전체 평균 스코어(ROUGE/BERT/G-EVAL)를 출력
    """
    print("\n===== Averages =====")

    # ROUGE 평균
    if "rouge" in results:
        rouge_list = results["rouge"]
        sums = {
            "r1_p": 0,
            "r1_r": 0,
            "r1_f": 0,
            "r2_p": 0,
            "r2_r": 0,
            "r2_f": 0,
            "rl_p": 0,
            "rl_r": 0,
            "rl_f": 0,
        }
        for item in rouge_list:
            p, r, f = item["rouge1"]
            sums["r1_p"] += p
            sums["r1_r"] += r
            sums["r1_f"] += f

            p, r, f = item["rouge2"]
            sums["r2_p"] += p
            sums["r2_r"] += r
            sums["r2_f"] += f

            p, r, f = item["rougeL"]
            sums["rl_p"] += p
            sums["rl_r"] += r
            sums["rl_f"] += f

        print("\n[ROUGE Avg]")
        print(
            f"  ROUGE-1  P: {sums['r1_p']/n_items:.4f}, "
            f"R: {sums['r1_r']/n_items:.4f}, "
            f"F1: {sums['r1_f']/n_items:.4f}"
        )
        print(
            f"  ROUGE-2  P: {sums['r2_p']/n_items:.4f}, "
            f"R: {sums['r2_r']/n_items:.4f}, "
            f"F1: {sums['r2_f']/n_items:.4f}"
        )
        print(
            f"  ROUGE-L  P: {sums['rl_p']/n_items:.4f}, "
            f"R: {sums['rl_r']/n_items:.4f}, "
            f"F1: {sums['rl_f']/n_items:.4f}"
        )

    # BERTScore 평균
    if "bert" in results:
        bert_list = results["bert"]
        p_sum = r_sum = f_sum = 0.0
        for p, r, f in bert_list:
            p_sum += p
            r_sum += r
            f_sum += f
        print("\n[BERT Avg]")
        print(f"  Precision: {p_sum/n_items:.4f}, " f"Recall: {r_sum/n_items:.4f}, " f"F1: {f_sum/n_items:.4f}")

    # G-EVAL 평균 (4가지 관점별 평균)
    if "g-eval" in results:
        geval_list = results["g-eval"]
        # consistency, coherence, fluency, relevance 각각의 합
        con_sum = coh_sum = flu_sum = rel_sum = 0.0

        for item in geval_list:
            con_sum += item["consistency"]
            coh_sum += item["coherence"]
            flu_sum += item["fluency"]
            rel_sum += item["relevance"]

        print("\n[G-EVAL Avg]")
        print(
            f"  consistency={con_sum/n_items:.4f}, "
            f"coherence={coh_sum/n_items:.4f}, "
            f"fluency={flu_sum/n_items:.4f}, "
            f"relevance={rel_sum/n_items:.4f}"
        )


def get_geval_scores(source_text, generated_text, config):
    """
    이메일과 이메일 요약문을 입력으로 받고
    4가지 관점("consistency", "coherence", "fluency", "relevance")에 대해 점수를 매긴다.

    source_text (str): 원본 이메일
    generated_text (str): 이메일 요약문

    Return:
        ({"consistency": float, "coherence": float, "fluency": float, "relevance": float},
        "점수: consistency=float, coherence=float, fluency=float, relevance=float")
        즉, 0번째는 dict, 1번째는 str

    """
    # g_eval 세팅
    g_eval_config = config.get("g_eval", {})
    prompt_files = g_eval_config.get("prompts_summary", {})
    model_name = g_eval_config.get("openai_model", "gpt-4")
    aspects = ["consistency", "coherence", "fluency", "relevance"]

    aspect_scores = {}
    for aspect in aspects:
        prompt_path = prompt_files.get(aspect, None)
        if not prompt_path:
            # 프롬프트 파일이 없다면 0으로 처리
            aspect_scores[aspect] = 0.0
            continue

        with open(prompt_path, "r", encoding="utf-8") as file:
            prompt_template = file.read()

        # 최종 프롬프트 선언
        if aspect == "fluency":
            prompt = PromptTemplate(
                input_variables=["Summary"],
                template=prompt_template,
            )

            formatted_prompt = prompt.format(
                Summary=generated_text,
            )
        else:
            prompt = PromptTemplate(
                input_variables=["Document", "Summary"],
                template=prompt_template,
            )

            formatted_prompt = prompt.format(Document=source_text, Summary=generated_text)

        # print("FORMATTED_PROMPT: ", formatted_prompt)
        # OpenAI API 호출
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": formatted_prompt}],
                temperature=0.7,
                max_tokens=50,
                n=1,
            )
            # GPT가 준 output을 float로 파싱
            gpt_text = response.choices[0].message.content.strip()
            score_value = float(gpt_text)
            aspect_scores[aspect] = score_value
        except Exception as e:
            print(f"[Error in G-EVAL] aspect={aspect}, error={e}")
            aspect_scores[aspect] = 0.0

    scores = []
    for key, value in aspect_scores.items():
        score_str = f"{key}={value}"
        scores.append(score_str)

    final_str = "점수: " + ", ".join(scores)

    return aspect_scores, final_str


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="eval_config.yml", help="Path to YAML config file")
    args = parser.parse_args()

    # 1) YAML 설정 로드
    config = load_config(args.config)

    json_file = config.get("json_file", None)
    metrics = config.get("metrics", [])

    if not json_file:
        raise ValueError("config.yml에 json_file 경로가 지정되지 않았습니다.")

    # 2) JSON 로드
    source_texts, summarized_texts, gold_texts = load_data_json(json_file)

    # 3) 길이 검사
    validate_data_lengths(metrics, source_texts, summarized_texts, gold_texts)

    # 4) 메트릭 계산
    results = compute_metrics(config, source_texts, summarized_texts, gold_texts)

    # 5) 결과 출력
    print("===== Evaluation Results =====")
    print_per_item_scores(results, source_texts, summarized_texts, gold_texts)
    print_averages(results, len(summarized_texts))


if __name__ == "__main__":
    main()
