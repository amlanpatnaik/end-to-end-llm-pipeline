import logging
import pandas as pd
import qwak
import yaml
from datasets import load_dataset, DatasetDict
from peft import LoraConfig, prepare_model_for_kbit_training, get_peft_model, PeftModel
from qwak.model.adapters import DefaultOutputAdapter
from qwak.model.base import QwakModel
import torch as th
from qwak.model.schema import ModelSchema
from qwak.model.schema_entities import RequestInput, InferenceOutput
from transformers import AutoTokenizer, DataCollatorForLanguageModeling, TrainingArguments, Trainer, BitsAndBytesConfig, \
    AutoModelForCausalLM, PreTrainedModel
from comet_ml import Experiment
from comet_ml.integration.pytorch import log_model
import os

from settings import settings


class CopywriterModel(QwakModel):
    def __init__(self, is_saved: bool = False, train_data_file: str = "./linkedin-train.json",
                 validation_data_file: str = "./linkedin-validation.json", model_save_dir: str = "./model",
                 model_type: str = "mistralai/Mistral-7B-Instruct-v0.1"):
        self._prep_environment()
        self.experiment = None
        self.data_files = {"train": train_data_file, "validation": validation_data_file}
        self.model_save_dir = model_save_dir
        self.model_type = model_type
        if is_saved:
            self.experiment = Experiment(
                api_key=settings.COMET_API_KEY,
                project_name=settings.COMET_PROJECT,
                workspace=settings.COMET_WORKSPACE
            )

    def _prep_environment(self):
        os.environ["TOKENIZERS_PARALLELISM"] = settings.TOKENIZERS_PARALLELISM
        th.cuda.empty_cache()
        logging.info("Emptied cuda cache. Environment prepared successfully!")

    def init_model(self):
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_type,
            token=settings.HUGGINGFACE_ACCESS_TOKEN,
            device_map=th.cuda.current_device(),
            quantization_config=self.nf4_config,
            use_cache=False,
            torchscript=True
        )
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_type, token=settings.HUGGINGFACE_ACCESS_TOKEN)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "right"
        logging.info(f'Initialized model{self.model_type} successfully')

    def _init_4bit_config(self):
        self.nf4_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=th.bfloat16
        )
        if self.experiment:
            self.experiment.log_parameters(self.nf4_config)
        logging.info("Initialized config for param representation on 4bits successfully!")

    def _initialize_qlora(self, model: PreTrainedModel) -> PeftModel:
        self.qlora_config = LoraConfig(
            lora_alpha=16,
            lora_dropout=0.1,
            r=64,
            bias="none",
            task_type="CAUSAL_LM"
        )

        if self.experiment:
            self.experiment.log_parameters(self.qlora_config)

        model = prepare_model_for_kbit_training(model)
        model = get_peft_model(model, self.qlora_config)
        logging.info("Initialized qlora config successfully!")
        return model

    def _init_trainig_args(self):
        with open('config.yaml', 'r') as file:
            config = yaml.safe_load(file)
        self.training_arguments = TrainingArguments(**config['training_arguments'])
        if self.experiment:
            self.experiment.log_parameters(self.training_arguments)
        logging.info("Initialized training arguments successfully!")

    def _remove_model_class_attributes(self):
        # remove needed in order to skip default serialization with Pickle done by Qwak
        del self.model
        del self.trainer

    def generate_prompt(self, sample: dict) -> dict:
        full_prompt = f"""<s>[INST]{sample['instruction']}
        [/INST] {sample['content']}</s>"""
        result = self.tokenize(full_prompt)
        return result

    def tokenize(self, prompt: str) -> dict:
        result = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=2300,
            truncation=True,
        )
        result["labels"] = result["input_ids"].copy()
        return result

    def load_dataset(self) -> DatasetDict:
        raw_datasets = load_dataset("json", data_files=self.data_files)
        train_data = raw_datasets['train']
        val_data = raw_datasets['validation']
        generated_train_dataset = train_data.map(self.generate_prompt)
        generated_train_dataset = generated_train_dataset.remove_columns(["instruction", "content"])
        generated_val_dataset = val_data.map(self.generate_prompt)
        generated_val_dataset = generated_val_dataset.remove_columns(["instruction", "content"])
        return DatasetDict({
            'train': generated_train_dataset,
            'validation': generated_val_dataset
        })

    def build(self):
        self._init_4bit_config()
        self.init_model()
        if self.experiment:
            self.experiment.log_parameters(self.nf4_config)
        self.model = self._initialize_qlora(self.model)
        self._init_trainig_args()
        tokenized_datasets = self.load_dataset()
        self.device = th.device("cuda" if th.cuda.is_available() else "cpu")
        self.model = self.model.to(self.device)
        self.trainer = Trainer(
            model=self.model,
            args=self.training_arguments,
            train_dataset=tokenized_datasets['train'],
            eval_dataset=tokenized_datasets['validation'],
            tokenizer=self.tokenizer,
        )
        logging.info("Initialized model trainer")
        self.trainer.train()
        if self.experiment:
            log_model(self.experiment, model=self.model, model_name="Copywriter")
        logging.info("Finished model finetuning!")
        self.trainer.save_model(self.model_save_dir)
        logging.info(f'Finished saving model to {self.model_save_dir}')
        self._remove_model_class_attributes()
        logging.info("Finished removing model class attributes!")

    def initialize_model(self):
        self.model = AutoModelForCausalLM.from_pretrained(self.model_save_dir, token=settings.HUGGINGFACE_ACCESS_TOKEN,
                                                          quantization_config=self.nf4_config)
        logging.info(f'Successfully loaded model from {self.model_save_dir}')

    def schema(self) -> ModelSchema:
        return ModelSchema(inputs=[RequestInput(name="instruction", type=str)],
                           outputs=[InferenceOutput(name="content", type=str)])

    @qwak.api(output_adapter=DefaultOutputAdapter())
    def predict(self, df):
        input_text = list(df['instruction'].values)
        input_ids = self.tokenizer(input_text, return_tensors="pt", add_special_tokens=True)
        input_ids = input_ids.to(self.device)

        generated_ids = self.model.generate(**input_ids, max_new_tokens=3000, do_sample=True,
                                            pad_token_id=self.tokenizer.eos_token_id)

        decoded_output = self.tokenizer.batch_decode(generated_ids)

        return pd.DataFrame([{"content": decoded_output}])
