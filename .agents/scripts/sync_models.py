import yaml
import requests
import os

# 1. Get models currently running in your local Ollama instance
def get_ollama_models():
    try:
        # Default Ollama API endpoint for listing local models
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code == 200:
            # Extract names from the list of models
            return [m['name'] for m in response.json().get('models', [])]
    except Exception as e:
        print(f"⚠️ Ollama not found or not running: {e}")
    return []

# 2. Update the LiteLLM config file
def sync_to_litellm():
    config_path = 'config.yaml' # Assumes config.yaml is in your project root
    
    # Load existing config or start a new one if missing
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {"model_list": []}
    else:
        config = {"model_list": []}

    local_models = get_ollama_models()

    # Clear existing 'local-' tagged models to avoid duplicates
    # This keeps your Cloud models (Claude/OpenAI) intact
    config['model_list'] = [m for m in config['model_list'] if not m.get('model_name', '').startswith('local-')]

    # Add each Ollama model as a 'local-' worker
    for model in local_models:
        # Clean name: 'llama3.1:latest' becomes 'local-llama3.1'
        display_name = f"local-{model.split(':')[0]}"
        
        config['model_list'].append({
            "model_name": display_name,
            "litellm_params": {
                "model": f"ollama/{model}",
                "api_base": "http://localhost:11434"
            }
        })

    # Save the updated manifest back to the file
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print(f"✅ Successfully synced {len(local_models)} Ollama models to {config_path}")

if __name__ == "__main__":
    sync_to_litellm()
