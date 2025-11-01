import unittest

import requests
import torch
import asyncio

import sglang as sgl
from sglang.srt.utils import kill_process_tree
from sglang.srt.utils.hf_transformers_utils import get_tokenizer
from sglang.utils import async_stream_and_merge
from sglang.test.test_utils import (
    DEFAULT_EAGLE_DRAFT_MODEL_FOR_TEST,
    DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST,
    DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST_EAGLE3,
    DEFAULT_MODEL_NAME_FOR_TEST_EAGLE3,
    DEFAULT_MODEL_NAME_FOR_TEST_MLA,
    DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
    DEFAULT_URL_FOR_TEST,
    CustomTestCase,
    is_in_ci,
    popen_launch_server,
)

torch_dtype = torch.float16
prefill_tolerance = 5e-2
decode_tolerance: float = 5e-2

DEFAULT_MODEL_NAME_FOR_TEST_EAGLE3 = "/shared/public/elr-models/jamesliu1/sglang-EAGLE3-Llama-3.1-Instruct-8B/e5ed08d66f528a95ce89f5d4fd136a28f6def714"
DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST_EAGLE3 = "/shared/public/elr-models/meta-llama/Meta-Llama-3.1-8B-Instruct/07eb05b21d191a58c577b4a45982fe0c049d0693"

async def async_stream_ramp_up(engine, prompts, params, tokens_per_request: int, tokenizer):
    outputs = [""] * len(prompts)
    token_counts = [0] * len(prompts)
    started = [False] * len(prompts)
    tasks = {}
    queue = asyncio.Queue()

    async def stream_one(i: int):
        async for cleaned_chunk in async_stream_and_merge(engine, prompts[i], params):
            outputs[i] += cleaned_chunk
            # Recompute tokens on full generated text to account for cross-boundary merges
            token_counts[i] = len(tokenizer.encode(outputs[i], truncation=False))
            await queue.put(("chunk", i, cleaned_chunk))
        await queue.put(("done", i, ""))

    # Kick off the first prompt
    started[0] = True
    tasks[0] = asyncio.create_task(stream_one(0))
    last_started = 0

    finished = 0
    accumulated_outputs = [""] * len(prompts)
    while finished < len(prompts):
        typ, i, payload = await queue.get()
        if typ == "chunk":
            # Accumulate chunks for later printing
            accumulated_outputs[i] += payload
            # When the last-started prompt reaches the token threshold, start the next prompt
            if i == last_started and token_counts[i] >= tokens_per_request:
                j = last_started + 1
                if j < len(prompts) and not started[j]:
                    started[j] = True
                    last_started = j
                    tasks[j] = asyncio.create_task(stream_one(j))
        else:
            finished += 1
            print(f"Output for prompt {i}: {accumulated_outputs[i]}")

    if tasks:
        await asyncio.gather(*tasks.values())
    return outputs

class TestEAGLEEngine(CustomTestCase):
    BASE_CONFIG = {
        "model_path": DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST,
        "speculative_draft_model_path": DEFAULT_EAGLE_DRAFT_MODEL_FOR_TEST,
        "speculative_algorithm": "EAGLE",
        "speculative_num_steps": 5,
        "speculative_eagle_topk": 4,
        "speculative_num_draft_tokens": 8,
        "mem_fraction_static": 0.7,
        "cuda_graph_max_bs": 5,
    }
    NUM_CONFIGS = 2

    THRESHOLDS = {
        "batch_avg_accept_len": 1.9,
        "accept_len": 3.6,
    }

    def setUp(self):
        self.prompt = "Today is a sunny day and I like"
        self.sampling_params = {"temperature": 0, "max_new_tokens": 8}

        ref_engine = sgl.Engine(
            model_path=self.BASE_CONFIG["model_path"], cuda_graph_max_bs=1
        )
        self.ref_output = ref_engine.generate(self.prompt, self.sampling_params)["text"]
        ref_engine.shutdown()

    def test_correctness(self):
        configs = [
            # Basic config
            self.BASE_CONFIG,
            # Chunked prefill
            {**self.BASE_CONFIG, "chunked_prefill_size": 4},
        ]

        for i, config in enumerate(configs[: self.NUM_CONFIGS]):
            with self.subTest(i=i):
                print(f"{config=}")
                engine = sgl.Engine(**config, log_level="info", decode_log_interval=10)
                try:
                    # self._test_single_generation(engine)
                    self._test_batch_generation(engine)
                    # self._test_eos_token(engine)
                    # self._test_acc_length(engine)
                finally:
                    engine.flush_cache()  # check engine alive
                    engine.shutdown()
                print("=" * 100)

    def _test_single_generation(self, engine):
        output = engine.generate(self.prompt, self.sampling_params)["text"]
        print(f"{output=}, {self.ref_output=}")
        self.assertEqual(output, self.ref_output)

    def _test_batch_generation(self, engine):
        prompts = [
            "Hello, my name is",
            "The president of the United States is",
            "The capital of France is",
            "The future of AI is",
        ]
        # params = {"temperature": 0.1, "top_p": 0.95, "max_new_tokens": 500, "frequency_penalty": 0.5}
        params = {"temperature": 0, "max_new_tokens": 100}
        print("Generated text: ", end="", flush=True)
        tokenizer = get_tokenizer(DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST_EAGLE3)
        asyncio.run(async_stream_ramp_up(engine, prompts, params, tokens_per_request=10, tokenizer=tokenizer))
        # outputs = engine.generate(prompts, params)
        # for prompt, output in zip(prompts, outputs):
            # print(f"Prompt: {prompt}")
            # print(f"Generated: {output['text']}")
            # print("-" * 40)

        avg_spec_accept_length = engine.get_server_info()["internal_states"][0][
            "avg_spec_accept_length"
        ]
        print(f"{avg_spec_accept_length=}")
        # self.assertGreater(
        #     avg_spec_accept_length, self.THRESHOLDS["batch_avg_accept_len"]
        # )

    def _test_eos_token(self, engine):
        prompt = "[INST] <<SYS>>\nYou are a helpful assistant.\n<</SYS>>\nToday is a sunny day and I like [/INST]"
        params = {
            "temperature": 0.1,
            "max_new_tokens": 1024,
            "skip_special_tokens": False,
        }

        tokenizer = get_tokenizer(DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST)
        output = engine.generate(prompt, params)["text"]
        print(f"{output=}")

        tokens = tokenizer.encode(output, truncation=False)
        self.assertNotIn(tokenizer.eos_token_id, tokens)

    def _test_acc_length(self, engine):
        prompt = [
            "Human: Give me a fully functional FastAPI server. Show the python code.\n\nAssistant:",
        ] * 5  # test batched generation
        sampling_params = {"temperature": 0, "max_new_tokens": 512}
        output = engine.generate(prompt, sampling_params)
        output = output[0]

        if "spec_verify_ct" in output["meta_info"]:
            acc_length = (
                output["meta_info"]["completion_tokens"]
                / output["meta_info"]["spec_verify_ct"]
            )
        else:
            acc_length = 1.0

        speed = (
            output["meta_info"]["completion_tokens"]
            / output["meta_info"]["e2e_latency"]
        )
        print(f"{acc_length=:.4f}, {speed=}")

        self.assertGreater(acc_length, self.THRESHOLDS["accept_len"])


class TestEAGLEEngineTokenMap(TestEAGLEEngine):
    BASE_CONFIG = {
        "model_path": "meta-llama/Meta-Llama-3-8B-Instruct",
        "speculative_draft_model_path": "lmsys/sglang-EAGLE-LLaMA3-Instruct-8B",
        "speculative_algorithm": "EAGLE",
        "speculative_num_steps": 5,
        "speculative_eagle_topk": 4,
        "speculative_num_draft_tokens": 8,
        "speculative_token_map": "thunlp/LLaMA3-Instruct-8B-FR-Spec/freq_32768.pt",
        "mem_fraction_static": 0.7,
        "cuda_graph_max_bs": 5,
        "dtype": "float16",
    }
    NUM_CONFIGS = 1
    THRESHOLDS = {
        "batch_avg_accept_len": 1.9,
        "accept_len": 2.5,
    }


class TestEAGLE3Engine(TestEAGLEEngine):
    import os
    base_gpu_id = int(os.environ.get("BASE_GPU_ID", 0))
    BASE_CONFIG = {
        "model_path": DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST_EAGLE3,
        "speculative_draft_model_path": DEFAULT_MODEL_NAME_FOR_TEST_EAGLE3,
        "speculative_algorithm": "EAGLE3",
        "speculative_num_steps": 2,
        "speculative_eagle_topk": 1,
        "speculative_num_draft_tokens": 3,
        "mem_fraction_static": 0.7,
        # "cuda_graph_max_bs": 5,
        "dtype": "float16",
        "disable_cuda_graph": True,
        "attention_backend": "fa3",
        "watchdog_timeout": 30000,
        "skip_server_warmup": True,
        "base_gpu_id": base_gpu_id,
        "enable_torch_compile": False,
    }
    NUM_CONFIGS = 1
    THRESHOLDS = {
        "batch_avg_accept_len": 1.75,
        "accept_len": 3.1,
    }


class TestEAGLERadixCache(CustomTestCase):
    BASE_CONFIG = {
        "model_path": DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST_EAGLE3,
        "speculative_draft_model_path": DEFAULT_MODEL_NAME_FOR_TEST_EAGLE3,
        "speculative_algorithm": "EAGLE3",
        "speculative_num_steps": 2,
        "speculative_eagle_topk": 1,
        "speculative_num_draft_tokens": 3,
        "mem_fraction_static": 0.7,
        "cuda_graph_max_bs": 5,
        "dtype": "float16",
    }

    def test_correctness(self):
        configs = [
            # Basic config
            self.BASE_CONFIG,
            # Chunked prefill
            {**self.BASE_CONFIG, "chunked_prefill_size": 64},
            # Chunked prefill & Page Size > 1
            {**self.BASE_CONFIG, "chunked_prefill_size": 64, "page_size": 4},
        ]

        for i, config in enumerate(configs):
            with self.subTest(i=i):
                engine = sgl.Engine(**config, log_level="info", decode_log_interval=1)
                try:
                    self._test_acc_length(engine)
                finally:
                    engine.shutdown()
                print("=" * 100)

    def _test_acc_length(self, engine):
        warmup_prompt = [
            "Human: Give me a fully functional FastAPI server. Show the python code.\n\nAssistant:",
        ]
        sampling_params = {"temperature": 0, "max_new_tokens": 512}
        output = engine.generate(warmup_prompt, sampling_params)
        test_prompt = [
            "<|start_header_id|>system<|end_header_id|>\n\nYou are a helpful, respectful and honest assistant. Always answer as helpfully as possible, while being safe.  Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content. Please ensure that your responses are socially unbiased and positive in nature.\n\nIf a question does not make any sense, or is not factually coherent, explain why instead of answering something not correct. If you don't know the answer to a question, please don't share false information.<|eot_id|><|start_header_id|>user<|end_header_id|>\n\nGive me a fully functional FastAPI server. Show the python code.<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
        ]
        output = engine.generate(test_prompt, sampling_params)
        output = output[0]

        if "spec_verify_ct" in output["meta_info"]:
            acc_length = (
                output["meta_info"]["completion_tokens"]
                / output["meta_info"]["spec_verify_ct"]
            )
        else:
            acc_length = 1.0

        speed = (
            output["meta_info"]["completion_tokens"]
            / output["meta_info"]["e2e_latency"]
        )
        print(f"{acc_length=:.4f}, {speed=}")

        self.assertGreater(acc_length, 2.5)


@unittest.skipIf(is_in_ci(), "To reduce the CI execution time.")
class TestEAGLEDraftExtend(CustomTestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = DEFAULT_URL_FOR_TEST
        cls.process = popen_launch_server(
            DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=[
                "--speculative-algorithm",
                "EAGLE",
                "--speculative-draft-model-path",
                DEFAULT_EAGLE_DRAFT_MODEL_FOR_TEST,
                "--speculative-num-steps",
                1,
                "--speculative-eagle-topk",
                1,
                "--speculative-num-draft-tokens",
                2,
                "--max-running-requests",
                4,
                "--attention-backend",
                "fa3",
            ],
        )
        cls.accept_len_threshold = 1.50

    @classmethod
    def tearDownClass(cls):
        kill_process_tree(cls.process.pid)

    def test_one_batch_accept_length(self):
        resp = requests.get(self.base_url + "/flush_cache")
        self.assertEqual(resp.status_code, 200)

        prompts = [
            "Hello, my name is",
            "The president of the United States is",
            "The capital of France is",
            "The future of AI is",
        ]
        url = self.base_url + "/generate"
        data = {
            "text": prompts,
            "sampling_params": {
                "temperature": 0,
                "max_new_tokens": 512,
            },
        }
        response = requests.post(url, json=data)
        self.assertEqual(response.status_code, 200)
        outputs = response.json()
        for i in range(len(prompts)):
            output = outputs[i]
            if "spec_verify_ct" in output["meta_info"]:
                acc_length = (
                    output["meta_info"]["completion_tokens"]
                    / output["meta_info"]["spec_verify_ct"]
                )
            else:
                acc_length = 1.0

            print(f"{acc_length=}")
            self.assertGreater(acc_length, self.accept_len_threshold)


class TestEAGLEDraftExtendFlashinfer(TestEAGLEDraftExtend):
    @classmethod
    def setUpClass(cls):
        cls.base_url = DEFAULT_URL_FOR_TEST
        cls.process = popen_launch_server(
            DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=[
                "--speculative-algorithm",
                "EAGLE",
                "--speculative-draft-model-path",
                DEFAULT_EAGLE_DRAFT_MODEL_FOR_TEST,
                "--speculative-num-steps",
                1,
                "--speculative-eagle-topk",
                1,
                "--speculative-num-draft-tokens",
                2,
                "--max-running-requests",
                4,
                "--attention-backend",
                "flashinfer",
            ],
        )
        cls.accept_len_threshold = 1.50


@unittest.skipIf(is_in_ci(), "To reduce the CI execution time.")
class TestEAGLEDraftExtendTriton(TestEAGLEDraftExtend):
    @classmethod
    def setUpClass(cls):
        cls.base_url = DEFAULT_URL_FOR_TEST
        cls.process = popen_launch_server(
            DEFAULT_EAGLE_TARGET_MODEL_FOR_TEST,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=[
                "--speculative-algorithm",
                "EAGLE",
                "--speculative-draft-model-path",
                DEFAULT_EAGLE_DRAFT_MODEL_FOR_TEST,
                "--speculative-num-steps",
                1,
                "--speculative-eagle-topk",
                1,
                "--speculative-num-draft-tokens",
                2,
                "--max-running-requests",
                4,
                "--attention-backend",
                "triton",
            ],
        )
        cls.accept_len_threshold = 1.50


@unittest.skipIf(is_in_ci(), "To reduce the CI execution time.")
class TestEAGLEDraftExtendFlashinferMLA(TestEAGLEDraftExtend):
    @classmethod
    def setUpClass(cls):
        cls.base_url = DEFAULT_URL_FOR_TEST
        cls.process = popen_launch_server(
            DEFAULT_MODEL_NAME_FOR_TEST_MLA,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=[
                "--speculative-algorithm",
                "EAGLE",
                "--speculative-num-steps",
                1,
                "--speculative-eagle-topk",
                1,
                "--speculative-num-draft-tokens",
                2,
                "--max-running-requests",
                4,
                "--attention-backend",
                "flashinfer",
            ],
        )
        cls.accept_len_threshold = 1.85


if __name__ == "__main__":
    unittest.main(defaultTest="TestEAGLE3Engine.test_correctness")
