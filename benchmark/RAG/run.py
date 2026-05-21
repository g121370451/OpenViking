import os
import sys
import json
import yaml
import importlib 
from argparse import ArgumentParser
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from src.core.logger import setup_logging
# ==========================================
# 1. Environment Initialization
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR

ov_config_path = os.path.join(SCRIPT_DIR, "ov.conf")
if os.path.exists(ov_config_path):
    os.environ["OPENVIKING_CONFIG_FILE"] = ov_config_path
    print(f"[Init] Auto-detected OpenViking config: {ov_config_path}")

try:
    from src.pipeline import BenchmarkPipeline 
    from src.core.vector_store import VikingStoreWrapper
    from src.core.llm_client import LLMClientWrapper 
except SyntaxError as e:
    print(f"\n[Fatal Error] Syntax error while importing modules: {e}")
    sys.exit(1)
except ImportError as e:
    print(f"\n[Fatal Error] Cannot import modules: {e}")
    print(f"Current sys.path: {sys.path}\n")
    sys.exit(1)

# ==========================================
# 2. Helper Functions
# ==========================================

def load_config(config_path):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def resolve_path(path_str, base_path):
    """
    Convert relative path to absolute path based on base_path.
    If path_str is already absolute, keep it unchanged.
    """
    if not path_str:
        return path_str
    if os.path.isabs(path_str):
        return path_str
    return os.path.normpath(os.path.join(base_path, path_str))

def _generate_bench_ov_conf(original_conf_path, search_limit=None):
    """Generate a temporary ov.conf with benchmark control parameters injected."""
    import hashlib

    with open(original_conf_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    if search_limit is not None:
        config['default_search_limit'] = search_limit

    temp_dir = Path(SCRIPT_DIR) / ".temp"
    temp_dir.mkdir(exist_ok=True)

    conf_hash = hashlib.md5(original_conf_path.encode('utf-8')).hexdigest()[:8]
    temp_conf_path = str(temp_dir / f"ov_bench_{conf_hash}.conf")

    with open(temp_conf_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

    return temp_conf_path

# ==========================================
# 3. Main Program
# ==========================================

def main():
    parser = ArgumentParser(description="Run RAG Benchmark (Smart Path Handling)")
    default_config_path = os.path.join(SCRIPT_DIR, "config/config.yaml")
    
    parser.add_argument("--config", default=default_config_path, 
                        help=f"Path to config file. Default: {default_config_path}")
    
    parser.add_argument("--step", choices=["all", "import", "gen", "eval", "gen+eval", "del"], default="all", 
                        help="Execution step: 'import' (Ingest), 'gen' (Retrieve+LLM), 'eval' (Judge), 'gen+eval' (Gen then Eval), or 'all'")

    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint if available")
    
    args = parser.parse_args()

    # --- B. Load and Parse Config ---
    config_path = os.path.abspath(args.config)
    print(f"[Init] Loading configuration from: {config_path}")
    
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f"[Error] {e}")
        return

    # --- C. Path Resolution ---
    print(f"[Init] Resolving paths relative to Project Root: {PROJECT_ROOT}")
    dataset_name = config.get('dataset_name', 'UnknownDataset')
    retrieval_topk = config.get('execution', {}).get('retrieval_topk', 5)
    
    format_vars = {
        'dataset_name': dataset_name,
        'retrieval_topk': retrieval_topk
    }
    
    path_keys = ['dataset_path', 'output_dir', 'vector_store', 'log_file', 'doc_output_dir']
    for key in path_keys:
        if key in config.get('paths', {}):
            original = config['paths'][key]
            rendered_path = original.format(**format_vars)
            resolved = resolve_path(rendered_path, PROJECT_ROOT)
            config['paths'][key] = resolved
            # print(f"  - {key}: {resolved}")

    # --- D. Initialize Components ---
    try:
        search_limit = config.get('vikingbot', {}).get('search_limit')
        if search_limit is not None and os.path.exists(ov_config_path):
            temp_conf_path = _generate_bench_ov_conf(ov_config_path, search_limit=search_limit)
            os.environ["OPENVIKING_CONFIG_FILE"] = temp_conf_path
            print(f"[Init] Injected search_limit={search_limit} into temporary ov.conf: {temp_conf_path}")

        logger = setup_logging(config['paths']['log_file'])
        logger.info(">>> Benchmark Session Started")
        
        # 1. Adapter (Dynamic Loading)
        adapter_cfg = config.get('adapter', {})
        module_path = adapter_cfg.get('module', 'src.adapters.locomo_adapter')
        class_name = adapter_cfg.get('class_name', 'LocomoAdapter')
        
        logger.info(f"Dynamically loading Adapter: {class_name} from {module_path}")
        logger.info(f"Loading dataset from: {config['paths']['dataset_path']}")
        
        try:
            mod = importlib.import_module(module_path)
            AdapterClass = getattr(mod, class_name)
            adapter = AdapterClass(raw_file_path=config['paths']['dataset_path'])
        except ImportError as e:
            logger.error(f"Could not import module '{module_path}'. Please check your config 'adapter.module'. Error: {e}")
            raise e
        except AttributeError as e:
            logger.error(f"Class '{class_name}' not found in module '{module_path}'. Please check your config 'adapter.class_name'. Error: {e}")
            raise e
        
        # 2. Vector Store
        use_nanobot = config.get('execution', {}).get('use_nanobot', False)
        if use_nanobot:
            vector_store = None
            logger.info("Nanobot mode: skipping VikingStoreWrapper initialization")
        else:
            vector_store = VikingStoreWrapper(store_path=config['paths']['vector_store'])
        
        # 3. LLM Client
        api_key = os.environ.get(
            config['llm'].get('api_key_env_var', ''), 
            config['llm'].get('api_key')
        )
        if not api_key:
            logger.warning("No API Key found in config or environment variables!")
            
        llm_client = LLMClientWrapper(config=config['llm'], api_key=api_key)

        # 4. Pipeline
        pipeline = BenchmarkPipeline(
            config=config,
            adapter=adapter,
            vector_db=vector_store,
            llm=llm_client,
            resume=args.resume,
        )

        # --- E. Execute Tasks ---
        if args.step in ["all", "import"]:
            logger.info("Stage: Import (Data Prepare + Ingest)")
            pipeline.run_import()
            
        if args.step in ["all", "gen", "gen+eval"]:
            logger.info("Stage: Generation (Retrieve + Generate)")
            pipeline.run_generation()
            
        if args.step in ["all", "eval", "gen+eval"]:
            logger.info("Stage: Evaluation (Judge -> Metrics)")
            pipeline.run_evaluation()

        if args.step in ["del"]:
            logger.info("Stage: Delete Vector Store")
            pipeline.run_deletion()
        
        logger.info("Benchmark finished successfully.")

    except KeyboardInterrupt:
        print("\n[Stop] Execution interrupted by user.")
    except Exception as e:
        if 'logger' in locals():
            logger.exception("Fatal error during execution")
        print(f"\n[Fatal Error] Program execution error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
