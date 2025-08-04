# Streamlit + LangGraph Agentic AI App

import streamlit as st
from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional, List
from langchain_core.runnables import Runnable
from serpapi import GoogleSearch
from vanna.remote import VannaDefault
from docx import Document
import tempfile
import os
from dotenv import load_dotenv
import json
import re
from openai import OpenAI
import pandas as pd
import sqlite3
from typing import Optional
import matplotlib.pyplot as plt
import networkx as nx
from io import BytesIO
from datetime import datetime
import json
from datetime import date, timedelta
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.dml.color import RGBColor
import io
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import OpenAIEmbeddings


st.set_page_config(layout="wide")

load_dotenv()

#os.environ["LANGCHAIN_TRACING_V2"]="true"
#os.environ["LANGCHAIN_API_KEY"]=os.getenv("LANGCHAIN_API_KEY")

embedding = OpenAIEmbeddings(openai_api_key=os.getenv("OPENAI_API_KEY"))
faiss_index = FAISS.load_local("faiss_index", embedding, allow_dangerous_deserialization=True)

# Keywords that usually indicate monetary columns
money_keywords = ["loss", "premium", "amount", "cost", "ibnr", "ult", "total", "claim", "reserve", "payment"]

# ---- Vanna Setup ----
vanna_api_key = st.secrets["vanna_api_key"]
vanna_model_name = st.secrets["vanna_model_name"]
vn_model = VannaDefault(model=vanna_model_name, api_key=vanna_api_key)
vn_model.connect_to_sqlite('Actuarial_Data.db')

# ---- Config ----
serpapi_key = st.secrets["SERPAPI_API_KEY"]

# ---Open AI LLM---

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

def call_llm(prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an intelligent AI assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=500
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"OpenAI call failed: {e}"


def last(old, new):
    return new

# ---- Define LangGraph State ----
class GraphState(TypedDict):
    user_prompt: str
    doc_loaded: bool
    document_path: Optional[str]
    vanna_prompt: Optional[str]
    fuzzy_prompt: Optional[str]
    route: Optional[str]
    sql_result: Optional[pd.DataFrame]
    web_links: Optional[List[str]]
    updated_doc_path: Optional[str]
    chart_info: Optional[dict]
    comparison_summary: Optional[str]
    general_summary: Optional[str]
    sql_query: Optional[str]
    faiss_summary: Optional[str]
    faiss_sources: Optional[list[tuple[str, str]]]
    faiss_images: Optional[list[dict]]


def get_schema_description(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    schema_str = ""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()

    for table_name, in tables:
        cursor.execute(f"PRAGMA table_info({table_name});")
        cols = cursor.fetchall()
        col_names = [col[1] for col in cols]
        schema_str += f"\n- {table_name}: columns = {', '.join(col_names)}"

    conn.close()
    return schema_str.strip()

def load_qs_pairs():
    with open("vanna_advanced_sql_pairs.txt", "r") as f:
        text = f.read()
    pairs = re.findall(r'question="(.*?)",\s*sql="""(.*?)"""', text, re.DOTALL)
    return [{"question": q.strip(), "sql": s.strip()} for q, s in pairs]

QSPairs = load_qs_pairs()
qs_examples = "\n".join(
    f"Q: {pair['question']}\nSQL: {pair['sql']}" for pair in QSPairs[:7]  # Limit to 7 to avoid token overflow
)

documentation = """
PnC_Data Table:
- Reserve Class contains insurance business lines such as 'Property', 'Casualty', 'Marine', 'Motor', etc.
- Exposure Year refers to the year in which the insured risk was exposed to potential loss.
- RI Type identifies whether the record is 'Gross' or one of the reinsurance types such as 'Ceded - XOL', 'Ceded - QS', 'Ceded - CAP', 'Ceded - FAC', or 'Ceded - Others'.
- Branch indicates the geographical business unit handling the contract, e.g., 'Europe', 'LATAM', 'North America'.
- Loss Type captures the nature of the loss, and may be one of: 'ATT', 'CAT', 'LARGE', 'THREAT', or 'Disc'.
- Underwriting Year represents the year in which the policy was underwritten or originated.
- Incurred Loss represents the total loss incurred to date, including paid and case reserves.
- Paid Loss is the portion of the Incurred Loss that has already been settled and paid out.
- IBNR is calculated as the difference between Ultimate Loss and Incurred Loss.
- Ultimate Loss is the projected final value of loss.
- Ultimate Premium refers to the projected premium expected to be earned.
- Loss Ratio is calculated as Ultimate Loss divided by Ultimate Premium.
- AvE Incurred = Expected - Actual Incurred.
- AvE Paid = Expected - Actual Paid.
- Budget Premium is the forecasted premium for budgeting.
- Budget Loss is the projected loss for budgeting.
- Earned Premium is the portion of the premium that has been earned.
- Case Reserves = Incurred Loss - Paid Loss.
"""

STATE_KEYS_SET_AT_ENTRY = [
    "user_prompt", 
    "doc_loaded", 
    "document_path", 
    "vanna_prompt", 
    "fuzzy_prompt",
    "route",
    "sql_result",
    "sql_query",
    "web_links",
    "updated_doc_path",
    "chart_info",
    "comparison_summary",
    "general_summary",
    "faiss_summary", 
    "faiss_sources",
    "faiss_images"
]


def prune_state(state: GraphState, exclude: List[str]) -> dict:
    return {k: v for k, v in state.items() if k not in exclude}


# ---- Router Node (with prompt generation) ----
class RouterNode(Runnable):
    def invoke(self, state: GraphState, config=None) -> GraphState:
        doc_flag = "yes" if state['doc_loaded'] else "no"
        schema = get_schema_description('Actuarial_Data.db')

        router_prompt = f"""
        You are an intelligent routing agent. Your job is to:
        1. Choose one of the paths: "sql", "search", "document", "comp", "faissdb" based on the user prompt.
        2. Choose:
        - "sql" if the user is asking a question about structured insurance data (e.g. claims, premiums, reserves, IBNR, trends, comparisons across years or products) or something that can be answered from the following database schema:
            {schema}
        - Use this additional documentation to better understand column meanings:
          {documentation}
        - Additionally, here are some examples of SQL-style questions and their corresponding queries (QSPairs):
          {qs_examples}
        -EVEN IF the user also says things like "plot", "draw", "visualize", "graph", "bar chart", etc. — that still means they want structured data **along with** a chart. SO route it to SQL
            Example: "Show me IBNR over years and plot a bar chart" → route = "sql"
        -Route it to "sql" if queries includes the below mentioned:
            - Asks for trends, breakdowns, or aggregations of internal metrics (e.g., IBNR, reserves, severity, premiums, earned/ultimate loss)
            - Ask for trends **within internal data only**
            - Compares **internal data over time or segments** (e.g., years, lines of business, regions)
            - Ask for charts or visualizations ("plot", "bar chart", etc.)
            - Does NOT involve external benchmarking
            Even if the prompt includes words like "compare" or "change", still route to SQL if the context is strictly internal.
        -If the route is "sql", include vanna_prompt, but don't include fuzzy_prompt
            -(eg: User Prompt is "Show me exposure year wise incurred loss and plot a graph", then 
            -vanna_prompt will be "Shoe me exposure year wise incurred loss".
            -Your work is to remove the noise and focus only on things that are required to generate sql query from vanna. SO remove all the extra stuffs out of the user prompt.


        3. "document" ONLY if a document is uploaded (Document Uploaded = yes) AND the question involves updating/reading a document.
        -If the route is "document", include:
            - "vanna_prompt": an SQL-style question to query structured data.
            - "fuzzy_prompt": a natural language description of the header or table to update.

        
        4. Choose "search" if:
            - The user is asking about general or external information
            - Involves real-time info, news, global economic trends, regulations
            - The query cannot be answered by internal structured data or uploaded document
        - If the route is "search", DO NOT include vanna_prompt or fuzzy_prompt.


        5.Choose "comp" when the user is comparing internal data against external data, competitors, or industry benchmarks.
            Examples include peer review, benchmarking, market positioning, or competitive ratios.

            Trigger words/phrases (especially relevant for Actuarial & Finance users):
            - "industry benchmark"
            - "market average"
            - "how do we compare to..."
            - "peer comparison"
            - "market trend vs ours"
            - "against competitors"
            - "vs industry"
            - "benchmarking analysis"
            - "loss ratio gap with peers"
            - "pricing differential with market"
            - "expense ratio compared to competition"
            - "where do we stand in market"
            - "relative to industry"
            - "competitive advantage in reserves"
            - "our severity vs others"
            - "compare to S&P average" / "AM Best stats" / "regulatory benchmark"
        -(e.g.,User Prompt is "Compare IBNR trends with industry benchmarks for exposure year 2025 ")
        - Return Vanna_prompt as well as "Show IBNR trends for exposure year 2025"
        -Do not include fuzzy_prompt
        -Only include relevant columns in vanna_prompt. Do not include ClaimNumber or ID columns unless the user specifically asks for them.

       
        6. Choose "faissdb" when:
        - The prompt asks about the Sparta platform, Earmark Template, Branch Adjustment Template/Module, Projects in Sparta, or any internal process or documentation.
        - The user seems to be referring to internal workflows, or knowledge base content.
        -Example prompts that should be routed to `"faissdb"`:
            - "What are the steps in the Branch Adjustment Module?"
            - "Explain how Earmark Template is used in our process."
            - "Can you summarize Projects in Sparta?"


        Return output strictly in valid JSON format using double quotes and commas properly.
        DO NOT include any trailing commas. Your JSON must be parseable by Python's json.loads().

        Examples:

        For SQL:
        {{
            "route": "sql",
            "vanna_prompt": "Show IBNR trends for exposure year 2025"
        }}

        For Document:
        {{
            "route": "document",
            "vanna_prompt": "SELECT policy_id, total_loss FROM policies WHERE year = 2024",
            "fuzzy_prompt": "Update the table under 'Loss Overview' for 2024"
        }}

        For Comp:
        {{
             "route": "comp",
             "vanna_prompt": "Show IBNR trends for exposure year 2025"
        }}

        For Search:
        {{
            "route": "search"
        }}

        For faissdb 
        {{
        "route": "faissdb"
        }}

        User Prompt: "{state['user_prompt']}"
        Document Uploaded: {doc_flag}
        """

        try:
            response = call_llm(router_prompt)
            #st.write("Route:", response)

            match = re.search(r'{.*}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                chart_info = parsed.get("chart_info")
            else:
                st.warning("LLM did not return valid JSON. Routing to 'search' as fallback.")
                parsed = {"route": "search"}

        except Exception as e:
            st.error(f"[RouterNode] LLM call failed: {e}")
            parsed = {"route": "search"}

        # ✅ Enforce safety: remove vanna_prompt if not 'document' or 'comp'
        if parsed.get("route") not in ["document", "comp", "sql", "faissdb"]:
            parsed["vanna_prompt"] = None
            parsed["fuzzy_prompt"] = None

        # ✅ Define chart_info only if needed
        chart_info = None
        
        return {
            **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
            "route": parsed.get("route"),
            "vanna_prompt": parsed.get("vanna_prompt"),
            "fuzzy_prompt": parsed.get("fuzzy_prompt"),
            "chart_info": chart_info
        }
    
# ---- Vanna SQL Node ----

def get_user_chart_type(prompt: str) -> Optional[str]:
    prompt = prompt.lower()
    if "bar chart" in prompt or "bar graph" in prompt:
        return "bar"
    elif "line chart" in prompt or "line graph" in prompt:
        return "line"
    elif "pie chart" in prompt or "pie graph" in prompt:
        return "pie"
    return None


def suggest_chart(df: pd.DataFrame) -> Optional[dict]:
    sample_data = df.head(5).to_dict(orient="list")
    prompt = f"""
    You are a data visualization assistant.

    Here is the top of a pandas DataFrame:
    {json.dumps(sample_data, indent=2)}

    Your task:
    - Identify a good chart (bar, line, or pie) that best represents this data.
    - Choose 1 column for the x-axis (categorical or time-based), and 1 or more numeric columns for the y-axis.
    - If multiple y columns are appropriate (e.g. IBNR, IncurredLoss), return them as a list.

    Return your answer in JSON like:
    {{ "type": "bar", "x": "ExposureYear", "y": ["IncurredLoss", "IBNR"] }}

    If no chart is suitable, return: "none"
    """

    reply = call_llm(prompt)
    match = re.search(r'{.*}', reply, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            return None
    return None


def plot_chart(df: pd.DataFrame, chart_info: dict):
    chart_type = chart_info.get("type", "bar")
    x = chart_info.get("x")
    y = chart_info.get("y")

    if isinstance(y, str):
        y = [y]  # Make it a list

    df_columns = list(df.columns)
    def match_col(col_name):
        for c in df_columns:
            if col_name.lower().replace(" ", "") in c.lower().replace(" ", ""):
                return c
        return None

    x_col = match_col(x)
    y_cols = [match_col(col) for col in y if match_col(col)]

    if not x_col or not y_cols:
        st.warning(f"Invalid chart columns: {x}, {y}")
        return

    st.subheader(f"{chart_type.capitalize()} Chart: {', '.join(y)} vs {x}")

    if chart_type == "bar":
        st.bar_chart(df.set_index(x_col)[y_cols])
    elif chart_type == "line":
        st.line_chart(df.set_index(x_col)[y_cols])
    elif chart_type == "pie" and len(y_cols) == 1:
        fig, ax = plt.subplots()
        df.groupby(x_col)[y_cols[0]].sum().plot.pie(ax=ax, autopct='%1.1f%%')
        ax.set_ylabel('')
        st.pyplot(fig)
    else:
        st.warning("Pie chart supports only one y column.")

def vanna_node(state: GraphState) -> GraphState:
    # Use user_prompt if vanna_prompt is not available
    prompt = state["vanna_prompt"]

    sql_query = vn_model.generate_sql(prompt)

    try:
        result = vn_model.run_sql(sql_query)
        if isinstance(result, pd.DataFrame):
            parsed_result = result
        elif isinstance(result, list):
            parsed_result = pd.DataFrame(result)
        else:
            parsed_result = pd.DataFrame([{"Result": str(result)}])
    except Exception as e:
        parsed_result = pd.DataFrame([{"Error": f"SQL Execution failed: {e}"}])

    return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "sql_result": parsed_result,
        "sql_query": sql_query
    }


# --Enhance Google Search--

def enhance_query(prompt: str) -> str:
    insurance_keywords = [
        "insurance", "insurer", "claim", "premium", "underwriting",
        "policy", "fraud", "broker", "actuary", "reinsurance", "coverage", "Actuarial", "reserving","P&L","Profit and Loss"
    ]
    
    if any(keyword in prompt.lower() for keyword in insurance_keywords):
        base_query = prompt
    else:
        base_query = f"In the insurance industry, {prompt}"
    
    # Add site filters to target trusted insurance-related domains
    domain_filter = "site:deloitte.com OR site:irdai.gov.in OR site:insurancebusinessmag.com OR site:swissre.com"
    
    return f"{base_query} {domain_filter}"
 
# --- SerpAPI Node --- 
def serp_node(state: GraphState) -> GraphState:
    query = enhance_query(state["user_prompt"])

    search = GoogleSearch({
        "q": query,
        "api_key": os.getenv("SERPAPI_API_KEY"),
        "num": 5
    })
    results = search.get_dict()

    links = []
    summaries = []

    if "organic_results" in results:
        for r in results["organic_results"][:5]:
            link = r.get("link")
            title = r.get("title", "Untitled").strip('"')
            snippet = r.get("snippet", "No summary available.").strip('"')
            if link:
                links.append(f"[{title}]({link})")
                summaries.append(snippet)

    if not links:
        links = ["No insurance-related results found or API limit reached."]
        summaries = [""]

    # ✨ Add LLM-generated general summary with numeric insights
    combined_text = "\n".join(summaries)

    # Build conditional prompt for COMP vs SERP node - COMP includes SQL snippet
    if "sql_query" in state and state["sql_query"]:
        sql_snippet = f"\n🧾 Internal SQL Query:\n{state['sql_query']}"
    else:
        sql_snippet = ""

    if "sql_result" in state and isinstance(state["sql_result"], pd.DataFrame):
        sql_snippet += f"\n\n📊 Top 5 rows of SQL Output:\n{state['sql_result'].head(5).to_markdown(index=False)}"

    # ✅ If COMP node passed Vanna data
    if sql_snippet:
        general_summary_prompt = f"""
        You are an insurance and actuarial analyst comparing internal company data with external web results.

        Use the following INTERNAL SQL DATA ONLY FOR CONTEXT. **Do not include internal tables or numbers in your output.**

        🧾 Internal SQL Query:
        {state['sql_query'] if 'sql_query' in state else ''}

        📊 Top 5 rows of SQL Output (reference only, do not display):
        {state['sql_result'].head(5).to_markdown(index=False) if isinstance(state['sql_result'], pd.DataFrame) else ''}

        ---

        Now, using only the following external web snippets, write a summary:

        🔍 Web Snippets:
        {combined_text}

        ---

        User Prompt:
        "{state['user_prompt']}"


        🔽 Your Task:
        - Summarize **only what is found in the external data**
        - DO NOT display the internal SQL data or repeat it
        - Be concise, no more than **6–8 lines**
        - Include **percentages, currency, loss ratios, IBNR**, and other KPIs found in the web
        - Avoid repeating full articles or sentences

        Output format:
        1. 📌 Start with a summary of overall findings.
        2. 🔢 Then list 3–4 **quantitative highlights**.
        3. 💬 End with any notable quote or number from a source if applicable.
        4. Can include a table with numerical insights as well, but not the internal data or tabular data. Only if you found it in external data.
        """
    else:
        # 🔁 Fall back to generic summary for plain search node
        general_summary_prompt = f"""
        You are an insurance and actuarial analyst.

        Your task is to extract **concise and numerically rich insights** from the following web snippets, in response to this user query:

        "{state['user_prompt']}"

        Snippets:
        {combined_text}

        Your summary should:
        - Be structured and no more than **6–8 lines**
        - Include **percentages**, **currency values**, **ratios**, **dates**, and **growth trends**
        - Mention key **KPIs** (e.g., IBNR, premiums, loss ratios, reserves)
        - Avoid repeating the snippets. Instead, **synthesize them**
        - If no numbers are found, say so explicitly

        Output format:
        1. 📌 Start with a summary of overall findings.
        2. 🔢 Then list 3–4 **quantitative highlights**.
        3. 💬 End with any notable quote or number from a source if applicable.
        4. Can include a table with numerical insights as well
        """

    general_summary = call_llm(general_summary_prompt)
    print("General summary generated:", general_summary)

    return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "web_links": list(zip(links, summaries)),
        "general_summary": general_summary
    }

# -------------Comp Node------------------
def comp_node(state: GraphState) -> GraphState:
    # Step 1: Run Vanna SQL
    vanna_prompt = state.get("vanna_prompt") or state["user_prompt"]
    sql_query = vn_model.generate_sql(vanna_prompt)

    try:
        result = vn_model.run_sql(sql_query)
        if isinstance(result, pd.DataFrame):
            parsed_result = result
        elif isinstance(result, list):
            parsed_result = pd.DataFrame(result)
        else:
            parsed_result = pd.DataFrame([{"Result": str(result)}])
    except Exception as e:
        parsed_result = pd.DataFrame([{"Error": f"SQL Execution failed: {e}"}])
    
    sql_df = parsed_result
    
    # Step 2: Run Serp Search
    serp_result = serp_node({**state, "sql_query": sql_query, "sql_result": sql_df})
    web_links = serp_result.get("web_links")

    external_summary = serp_result.get("general_summary", "")

    # Step 3: Generate comparison summary using LLM
    summary_prompt = f"""
    You are an actuarial analyst comparing internal structured data with external insurance insights.

    Your job is to:
    1. Analyze differences, similarities, and gaps between internal company data and external web sources.
    2. Focus heavily on **numerical metrics** such as:
    - IBNR, Incurred Loss, Ultimate Loss
    - Premiums, Loss Ratios
    - Exposure Years, Percent changes

    3. Highlight:
    - Trends (increases/decreases)
    - Matching vs. diverging figures
    - Numerical differences or % differences

    🧾 Internal SQL Output (Top 5 rows, tabular format):
    {sql_df.head(5).to_markdown(index=False) if isinstance(sql_df, pd.DataFrame) else str(sql_df)}

    🌐 External Web Insights:
    {chr(10).join([f"- {title}: {summary[:200]}..." for title, summary in web_links])}

    💬 General Summary:
    {external_summary}

    Return your final answer as a **clearly structured comparison**.
    Prefer a short table or bullet points with side-by-side numbers wherever appropriate.
    Start with a one-liner summary, then details.
    """

    #summary_prompt += f"\n\nGeneral Summary from external web links:\n{external_summary}"
    
    comparison_summary = call_llm(summary_prompt)

    return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "sql_result": sql_df,
        "sql_query": sql_query,
        "web_links": web_links,
        "general_summary": serp_result.get("general_summary", ""),
        "comparison_summary": comparison_summary
    }



# faissdb node to extract internal docs
def faissdb_node(state: GraphState) -> GraphState:
    faiss = FAISS.load_local(
        folder_path="faiss_index/",
        embeddings=OpenAIEmbeddings(),
        allow_dangerous_deserialization=True
    )
    docs = faiss_index.similarity_search(state["user_prompt"], k=3)

    top_docs = docs[:3]  # ⬅️ Top 3 instead of 5
    content_snippets = "\n\n---\n\n".join(d.page_content[:500] for d in top_docs)

    summary_prompt = f"""
    Based on the following retrieved document chunks from internal knowledge base, answer the user's query:

    User Prompt: {state['user_prompt']}

    Documents:
    {content_snippets}

    Provide a concise and structured answer with key points or numeric details if found.
    """
    summary = call_llm(summary_prompt)

    # Extract faiss_sources with source path
    faiss_sources = []
    all_images = []

    for doc in top_docs:
        doc_name = doc.metadata.get("source_doc", "Doc")
        snippet = doc.page_content[:300]
        path = doc.metadata.get("file_path")  # must be present in ingestion step
        #print(f"[DEBUG] FAISS doc metadata: {doc.metadata}")
        faiss_sources.append((doc_name, snippet, path))

        # Load associated images
        image_meta_path = os.path.join("extracted_images", "extracted_image_metadata.json")
        if os.path.exists(image_meta_path):
            with open(image_meta_path, 'r') as f:
                all_metadata = json.load(f)
            related_images = [
                meta for meta in all_metadata
                if meta["original_doc"] == doc_name
            ]
            all_images.extend(related_images)

    return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "faiss_summary": summary,
        "faiss_sources": faiss_sources,
        "faiss_images": all_images
    }




# ---- Document Table Update Node ----
def document_node(state: GraphState) -> GraphState:
    doc_path = state['document_path']
    if not doc_path or not state.get("vanna_prompt"):
        return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "updated_doc_path": None
    }

    doc = Document(doc_path)

    structure_string = ""
    header = None
    header_table_map = {}

    for para in doc.paragraphs:
        if para.style.name.startswith("Heading"):
            header = para.text.strip()
            structure_string += f"\n# {header}"
            header_table_map[header] = []
        elif header:
            header_table_map[header].append(len(header_table_map[header]))

    for idx, table in enumerate(doc.tables):
        cols = [cell.text.strip() for cell in table.rows[0].cells]
        structure_string += f"\n- Table {idx}: {len(table.rows)} rows x {len(cols)} columns, Columns: {cols}"

    prompt = f"""
        You are helping identify the correct table to update in a Word document.
        Each table has: index, rows x cols, and list of column headers.

        Document structure:
        {structure_string}

        Instruction:
        \"\"\"{state['fuzzy_prompt']}\"\"\"

        Return strictly in JSON:
        {{ "header_text": "...", "table_index_under_header": 0 }}
    """
    llm_output = call_llm(prompt)
    json_match = re.search(r'{.*}', llm_output, re.DOTALL)
    parsed = json.loads(json_match.group()) if json_match else {"header_text": list(header_table_map)[0], "table_index_under_header": 0}

    # 💡 Generate SQL via Vanna (clean)
    try:
        sql_query = vn_model.generate_sql(state["vanna_prompt"])
        vanna_output = vn_model.run_sql(sql_query)
    except Exception as e:
        return {**state, "updated_doc_path": None, "error": f"SQL generation or execution failed: {e}"}

    # 📝 Update the correct table
    header = parsed['header_text']
    table_idx = parsed['table_index_under_header']
    matched_table_index = list(header_table_map[header])[table_idx]
    table = doc.tables[matched_table_index]

    # 🔁 Fill table with SQL output
    if isinstance(vanna_output, pd.DataFrame):
        for i, row in enumerate(vanna_output.itertuples(index=False), start=1):
            for j, value in enumerate(row):
                if i < len(table.rows) and j < len(table.columns):
                    table.cell(i, j).text = str(value)

    updated_path = "updated_doc.docx"
    doc.save(updated_path)

    return {
        **prune_state(state, STATE_KEYS_SET_AT_ENTRY),
        "updated_doc_path": updated_path,
        "header_updated": header,
        "table_index_updated": matched_table_index
    }



def generate_follow_up_questions(user_prompt: str) -> List[str]:
    followup_prompt = f"""
    Based on the following insurance-related user query:
    "{user_prompt}"

    Suggest 3 intelligent follow-up questions the user could ask next. Keep them short, relevant, and not repetitive.
    Return them as a plain list.
    """
    try:
        response = call_llm(followup_prompt)
        return re.findall(r"^\s*[-–•]?\s*(.+)", response, re.MULTILINE)[:3] or response.split("\n")[:3]
    except:
        return []



def visualize_workflow(builder, active_route=None):

    route_to_node = {
        "sql": "vanna_sql",
        "search": "serp_search",
        "document": "doc_update",
        "faissdb": "faissdb",
        "comp": "comp"
    }

    highlight_node = route_to_node.get(active_route)

    G = nx.DiGraph()
    edge_styles = {}

    # Add all nodes
    for node in builder.nodes:
        G.add_node(node)
    G.add_node("__start__")
    G.add_node("__end__")

    # LangGraph base edges
    for source, target in builder.edges:
        G.add_edge(source, target)
        # Add style only for non-router edges
        if source != "router":
            edge_styles[(source, target)] = {"style": "solid", "color": "black", "width": 1.5}

    # Always show dashed edges from router to all 3 branches
    for target in ["vanna_sql", "serp_search", "doc_update", "comp", "faissdb"]:
        if ("router", target) not in G.edges:
            G.add_edge("router", target)
        edge_styles[("router", target)] = {"style": "dashed", "color": "gray", "width": 1}

    # Highlight the active route in red
    if highlight_node:
        edge_styles[("router", highlight_node)] = {"style": "solid", "color": "red", "width": 2.5}

    # Positions for nodes
    pos = {
        "__start__": (2, 4),
        "router": (2, 3),
        "vanna_sql": (0, 2),
        "serp_search": (1, 2),
        "doc_update": (2, 2),
        "comp": (3, 2),
        "faissdb": (4, 2),
        "__end__": (2, 1),
    }

    plt.figure(figsize=(6, 5))
    nx.draw_networkx_nodes(G, pos, node_size=2500, node_color="skyblue")
    nx.draw_networkx_labels(G, pos, font_size=10, font_weight="bold")

    # Draw styled edges
    for edge in G.edges:
        style = edge_styles.get(edge, {"style": "solid", "color": "black", "width": 1})
        nx.draw_networkx_edges(
            G, pos,
            edgelist=[edge],
            arrows=True,
            arrowstyle='-|>',
            style=style["style"],
            edge_color=style["color"],
            width=style["width"]
        )

    plt.title("Agentic LangGraph Workflow")
    plt.axis("off")
    plt.tight_layout()
    st.pyplot(plt)


#Exporting data to Powerpoint
def generate_ppt(entry) -> BytesIO:
    prs = Presentation()
    layout = prs.slide_layouts[5]  # title + content

    # 🧠 Title Slide
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Agentic AI Report"
    slide.placeholders[1].text = f"Prompt: {entry['prompt']}"

    route = entry.get("route")

    # 🧾 SQL Query Slide (if applicable)
    if route in ["sql", "document", "comp"] and entry.get("sql_query"):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = "SQL Query"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5))
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = entry["sql_query"]
        p.font.size = Pt(14)

    # 📊 SQL Result Table (if applicable)
    result = entry.get("result")
    if isinstance(result, list):
        result = pd.DataFrame(result)

    if route in ["sql", "document", "comp"] and isinstance(result, pd.DataFrame) and not result.empty:
        df = pd.DataFrame(entry["result"]) if isinstance(entry["result"], list) else entry["result"]
        if isinstance(df, pd.DataFrame):
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = "SQL Results"
            rows = min(6, len(df) + 1)
            cols = len(df.columns)
            table = slide.shapes.add_table(rows, cols, Inches(0.5), Inches(1.2), Inches(8.5), Inches(3)).table
            for i, col in enumerate(df.columns):
                table.cell(0, i).text = str(col)
            for i, row in df.head(5).iterrows():
                for j, val in enumerate(row):
                    table.cell(i + 1, j).text = str(val)

    # 🆚 Comparison Summary
    if route == "comp" and entry.get("comparison_summary"):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = "Comparison Summary"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5))
        tf = box.text_frame
        tf.word_wrap = True
        for para in entry["comparison_summary"].split("\n"):
            if para.strip():
                p = tf.add_paragraph()
                p.text = para.strip()
                p.font.size = Pt(14)
                p.space_after = Pt(4)

    # 🧠 General Summary (Search + Comp)
    if route in ["search", "comp"] and entry.get("general_summary"):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = "General Summary"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5))
        tf = box.text_frame
        tf.word_wrap = True
        for para in entry["general_summary"].split("\n"):
            if para.strip():
                p = tf.add_paragraph()
                p.text = para.strip()
                p.font.size = Pt(14)
                p.space_after = Pt(4)

    # 🔗 Top Web Links (Search + Comp)
    if route in ["search", "comp"] and entry.get("web_links"):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = "Top Web Links"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5))
        tf = box.text_frame
        tf.word_wrap = True

        for i, (link_md, summary) in enumerate(entry["web_links"], 1):
            # Match Markdown-style link: [Title](https://link)
            match = re.match(r"\[(.*?)\]\((.*?)\)", link_md)
            if match:
                title, url = match.groups()
            else:
                title, url = f"Link {i}", link_md  # fallback

            # Add hyperlink paragraph
            p = tf.add_paragraph()
            run = p.add_run()
            run.text = f"{i}. {title}"
            run.font.size = Pt(13)
            run.hyperlink.address = url
            p.space_after = Pt(2)

            # Add summary (not a hyperlink)
            summary_p = tf.add_paragraph()
            summary_p.text = f"    ↳ {summary[:180]}..."
            summary_p.font.size = Pt(12)
            summary_p.space_after = Pt(6)

    if route == "faissdb":
        # 🧠 faiss Summary Slide
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = "FAISS Summary"

        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5.0))
        tf = box.text_frame
        tf.word_wrap = True

        summary_text = entry.get("faiss_summary", "No summary available.")
        for para in summary_text.split("\n"):
            if para.strip():
                p = tf.add_paragraph()
                p.text = para.strip()
                p.font.size = Pt(14)
                p.space_after = Pt(4)

        # 📄 Source Slides (with clickable file name in title if available)
        for i, (docname, snippet, path) in enumerate(entry.get("faiss_sources", []), 1):
            filename = os.path.basename(path) if path else docname
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = f"Source {i}: {filename}"

            box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(8.5), Inches(5.0))
            tf = box.text_frame
            tf.word_wrap = True

            for para in snippet.split("\n"):
                if para.strip():
                    p = tf.add_paragraph()
                    p.text = para.strip()
                    p.font.size = Pt(12)
                    p.space_after = Pt(3)

        # 🖼️ Image Slide (only from the most similar document)
        faiss_images = entry.get("faiss_images", [])
        faiss_sources = entry.get("faiss_sources", [])
        if faiss_images and faiss_sources:
            top_docname = faiss_sources[0][0]  # first doc's name
            top_doc_images = [img for img in faiss_images if img.get("original_doc") == top_docname]

            if top_doc_images:
                slide = prs.slides.add_slide(prs.slide_layouts[5])  # blank layout
                slide.shapes.title.text = f"Images from {top_docname}"

                # Adjust layout
                left = Inches(0.8)
                top = Inches(2.5)  # ⬅️ Increased top margin
                image_width = Inches(6)  # ⬅️ Increased width
                spacing = Inches(0.6)

                for idx, img_meta in enumerate(top_doc_images):
                    img_path = img_meta.get("extracted_image_path")
                    if img_path and os.path.exists(img_path):
                        slide.shapes.add_picture(img_path, left, top, width=image_width)
                        top += Inches(3.2)  # ⬅️ Increased vertical spacing

                        if top > Inches(6.5):  # wrap to next column if needed
                            top = Inches(2.0)
                            left += image_width + spacing


    # Finalize PPT in memory
    ppt_bytes = BytesIO()
    prs.save(ppt_bytes)
    ppt_bytes.seek(0)
    return ppt_bytes



# ---- LangGraph Setup ----
graph_builder = StateGraph(GraphState)
graph_builder.add_node("router", RouterNode())
graph_builder.add_node("vanna_sql", vanna_node)
graph_builder.add_node("serp_search", serp_node)
graph_builder.add_node("doc_update", document_node)
graph_builder.add_node("comp", comp_node)
graph_builder.add_node("faissdb", faissdb_node)

def router_logic(state: GraphState):
    if state['route'] == 'sql': return "vanna_sql"
    elif state['route'] == 'search': return "serp_search"
    elif state['route'] == 'document': return "doc_update"
    elif state['route'] == 'comp': return "comp"
    elif state['route'] == 'faissdb': return "faissdb"
    else: return END    

graph_builder.set_entry_point("router")

# ✅ Execution routing
graph_builder.add_conditional_edges("router", router_logic)

# ✅ Visualization support — add all potential router paths
#graph_builder.add_edge("router", "vanna_sql")
#graph_builder.add_edge("router", "serp_search")
#graph_builder.add_edge("router", "doc_update")

# Regular path to end
graph_builder.add_edge("vanna_sql", END)
graph_builder.add_edge("serp_search", END)
graph_builder.add_edge("doc_update", END)
graph_builder.add_edge("comp", END)
graph_builder.add_edge("faissdb", END)

agent_graph = graph_builder.compile()

# ---- Streamlit UI ----
st.title("\U0001F9E0 Agentic AI Assistant (Insurance)")


def format_date_label(chat_date: date) -> str:
    today = date.today()
    if chat_date == today:
        return "Today"
    elif chat_date == today - timedelta(days=1):
        return "Yesterday"
    else:
        return chat_date.strftime("%d %b %Y")
    
def generate_title(prompt: str) -> str:
    try:
        title_prompt = f"Summarize the following user query into a short title:\n\n'{prompt}'\n\nKeep it under 7 words."
        return call_llm(title_prompt)
    except:
        return prompt[:40] + ("..." if len(prompt) > 40 else "")
    

# ✅ Initialize chat history and active index
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "active_chat_index" not in st.session_state:
    st.session_state.active_chat_index = None

# ✅ Sidebar: Clear + View + Export
with st.sidebar:
    st.header("🗂️ Session")
    if st.button("🧹 Clear Chat History"):
        st.session_state.chat_history = []
        st.session_state.active_chat_index = None
        st.success("Chat history cleared!")

# ✅ Group and render chat history in sidebar
grouped = {}
for chat in st.session_state.chat_history:
    chat_date = datetime.strptime(chat["timestamp"], "%d %b %Y, %I:%M %p").date()
    grouped.setdefault(chat_date, []).append(chat)

for group_date in sorted(grouped.keys(), reverse=True):
    label = format_date_label(group_date)
    with st.sidebar.expander(f"📅 {label}"):
        entries = sorted(grouped[group_date], key=lambda e: not e.get("pinned", False))
        for idx, chat in enumerate(entries):
            title = chat.get("title") or chat["prompt"][:40]
            pin_icon = "📌 " if chat.get("pinned") else ""
            if st.button(f"{pin_icon}{title}", key=f"chat_{group_date}_{idx}"):
                st.session_state.active_chat_index = st.session_state.chat_history.index(chat)
                st.session_state.user_prompt = chat["prompt"]
                st.session_state.just_ran_agent = False

# ✅ Export chat history
def serialize_chat_history(history):
    safe_history = []
    for chat in history:
        safe_chat = chat.copy()
        if isinstance(safe_chat.get("result"), pd.DataFrame):
            safe_chat["result"] = safe_chat["result"].to_dict(orient="records")
        safe_history.append(safe_chat)
    return json.dumps(safe_history, indent=2)

history_json = serialize_chat_history(st.session_state.chat_history)

st.download_button("⬇️ Export Chat History", history_json, file_name="chat_history.json")

# Render before running agent (all dashed)
#with st.expander("🧭 Workflow Graph (Initial)"):
#    visualize_workflow(graph_builder)

# ✅ Initialize just_ran_agent flag if not already
if "just_ran_agent" not in st.session_state:
    st.session_state.just_ran_agent = False

# ✅ UI Control Logic: if user is NOT viewing past chat
if st.session_state.active_chat_index is None:
    with st.form(key="query_form"):
        user_prompt = st.text_input("Enter your query:")
        doc_file = st.file_uploader("Upload Insurance Document (.docx)", type=["docx"])
        submitted = st.form_submit_button("Run Agent")
    #user_prompt = st.text_input("Enter your query:", key="user_prompt")
    #doc_file = st.file_uploader("Upload Insurance Document (.docx)", type=["docx"])


    if submitted:
    # Only run when prompt is entered and changed
    #if user_prompt and (
    #    "last_prompt" not in st.session_state
    #    or st.session_state["last_prompt"] != user_prompt
    #):
        st.session_state["last_prompt"] = user_prompt

        if doc_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                tmp.write(doc_file.read())
                doc_path = tmp.name
        else:
            doc_path = None

        state: GraphState = {
            "user_prompt": user_prompt,
            "doc_loaded": doc_path is not None,
            "document_path": doc_path,
            "vanna_prompt": None,
            "fuzzy_prompt": None,
            "route": None,
            "sql_result": None,
            "sql_query": None,
            "web_links": None,
            "updated_doc_path": None,
            "comparison_summary": None,
            "general_summary": None,
            "faiss_summary": None,
            "faiss_sources": None,
            "faiss_images": None
        }

        with st.spinner("Running Agent..."):
            try:
                output = agent_graph.invoke(state)
                st.session_state.output = output
                followups = generate_follow_up_questions(user_prompt)
                st.session_state.followups = followups

            except Exception as e:
                st.error(f"Agent crashed due to error: {e}")
                import traceback
                st.code(traceback.format_exc())
                st.stop()

        # ✅ Save to chat history
        chat_entry = {
            "prompt": user_prompt,
            "title": generate_title(user_prompt),
            "route": output.get("route"),
            "result": output.get("sql_result") if output.get("route") in ["sql", "document", "comp"] else output.get("web_links"),
            "sql_query": output.get("sql_query"),
            "web_links": output.get("web_links"),
            "general_summary": output.get("general_summary"),
            "comparison_summary": output.get("comparison_summary"),
            "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "faiss_summary": output.get("faiss_summary"),
            "faiss_sources": output.get("faiss_sources"),
            "faiss_images": output.get("faiss_images")
        }

        st.session_state.chat_history.append(chat_entry)
        st.session_state.active_chat_index = len(st.session_state.chat_history) - 1
        st.session_state.just_ran_agent = True

        col_left, col_mid, col_right = st.columns([4, 0.4 ,1.5])

        with col_right:
            if st.session_state.get("output"):
                st.markdown("### 🧭 Workflow Diagram")
                visualize_workflow(graph_builder, active_route=st.session_state["output"].get("route"))


        with col_left:
            # ✅ Output rendering
            if output.get("route") in ["sql", "document", "comp"] and output.get("sql_result") is not None:
                st.subheader("SQL Query Result:")
                if output.get("sql_query"):  # For live session
                    st.code(output["sql_query"], language="sql")
                try:
                    sql_df = output["sql_result"]
                    if isinstance(sql_df, pd.DataFrame):
                        formatted_df = sql_df.copy()
                        for col in formatted_df.select_dtypes(include='number').columns:
                            col_lower = col.lower()
                            if "ratio" in col_lower:
                                formatted_df[col] = formatted_df[col].apply(lambda x: f"{x * 100:.2f}%" if pd.notnull(x) else "")
                            elif any(keyword in col_lower for keyword in money_keywords):
                                formatted_df[col] = formatted_df[col].apply(lambda x: f"{x:,.0f}")

                        st.dataframe(formatted_df)
                    else:
                        st.write("Raw SQL output:")
                        st.write(sql_df)
                except Exception as e:
                    st.warning(f"Could not display table properly: {e}")
                    st.write(output["sql_result"])

                if any(word in output["user_prompt"].lower() for word in ["plot", "draw", "visualize", "chart", "bar graph", "line graph", "pie chart", "graph"]):
                    user_chart_type = get_user_chart_type(output["user_prompt"])
                    chart_info = suggest_chart(sql_df)
                    
                    if chart_info and user_chart_type:
                        chart_info["type"] = user_chart_type

                    if chart_info:
                        try:
                            plot_chart(sql_df, chart_info)
                        except Exception as e:
                            st.warning(f"Could not render chart: {e}")

            if output.get("route") in ["search", "comp"] and output.get("web_links"):
                st.subheader("🧠 General Summary:")
                summary = output.get("general_summary")
                if summary and summary.strip().lower() != "none":
                    st.markdown(summary)
                else:
                    st.markdown("_No summary could be generated from the results._")

                st.subheader("🔗 Top Web Links:")
                for i, (link, summary) in enumerate(output["web_links"], 1):
                    st.markdown(f"**{i}.** {link}")
                    st.markdown(f"_Summary:_\n{summary}")

            if output.get("route") == "comp" and output.get("comparison_summary"):
                st.subheader("🆚 Comparison Summary:")
                st.markdown(output["comparison_summary"])
            
            if output.get("route") == "faissdb":
                st.subheader("📘 Internal Knowledge Base Answer:")
                st.markdown(output.get("faiss_summary", "_No summary available._"))

                # Show images related to the most similar doc
                if output.get("faiss_images"):
                    most_similar_doc = output["faiss_sources"][0][0]  # get docname
                    st.subheader(f"🖼️ Images from: {most_similar_doc}")
                    for meta in output["faiss_images"]:
                        if meta["original_doc"] == most_similar_doc:
                            img_path = meta["extracted_image_path"]
                            if img_path and os.path.exists(img_path):
                                st.image(img_path, caption=meta.get("caption", ""), use_container_width=True)

                st.subheader("📄 Document Sources:")
                for i, (docname, snippet, path) in enumerate(output.get("faiss_sources", []), 1):
                    st.markdown(f"**{i}. {docname}**\n\n{snippet}")
                    #st.code(f"📁 File path: {path}")
                    #st.code(f"🧪 Exists: {os.path.exists(path) if path else 'No path'}")
                    if path and os.path.exists(path):
                        with open(path, "rb") as f:
                            st.download_button(
                                label=f"📥 Download {os.path.basename(path)}",
                                data=f,
                                file_name=os.path.basename(path),
                                key=f"download_doc_{i}"
                            )




            if output.get("updated_doc_path"):
                with open(output["updated_doc_path"], "rb") as f:
                    st.download_button("Download Updated Document", f, file_name="updated.docx")

            if st.session_state.get("followups"):
                st.markdown("### 💬 You could also ask:")
                for q in followups:
                    st.markdown(f"- 👉 {q}")

            st.download_button("⬇️ Export to PPT", generate_ppt(chat_entry), file_name="agentic_ai_output.pptx")

            st.session_state.just_ran_agent = False
            st.session_state.active_chat_index = None




else:
    # ✅ If user is viewing previous chat, show message + unlock option
    st.info("📜 You're viewing a previous conversation. Click below to start a new query.")
    if st.button("Start New Query"):
        st.session_state.active_chat_index = None
        st.session_state.user_prompt = ""
        st.rerun() 

# ✅ Render selected chat in main area
if st.session_state.active_chat_index is not None and not st.session_state.just_ran_agent:
    entry = st.session_state.chat_history[st.session_state.active_chat_index]
    st.markdown(f"### 📝 Prompt\n{entry['prompt']}")
    st.caption(f"🕒 {entry['timestamp']}")
    st.markdown(f"_Route_: `{entry['route']}`")

    if entry["route"] in ["sql", "document"]:
        st.subheader("SQL Query Result:")
        if entry.get("sql_query"):  # For history view
            st.code(entry["sql_query"], language="sql")
        result_df = entry.get("result")
        if isinstance(result_df, list):  # was serialized
            result_df = pd.DataFrame(result_df)
        if isinstance(result_df, pd.DataFrame):
            formatted_df = result_df.copy()
            for col in formatted_df.select_dtypes(include='number').columns:
                col_lower = col.lower()
                if "ratio" in col_lower:
                    formatted_df[col] = formatted_df[col].apply(lambda x: f"{x * 100:.2f}%" if pd.notnull(x) else "")
                elif any(keyword in col_lower for keyword in money_keywords):
                    formatted_df[col] = formatted_df[col].apply(lambda x: f"{x:,.0f}")
            st.dataframe(formatted_df)
        else:
            st.text(result_df)

    elif entry["route"] == "faissdb":
        st.subheader("📘 Internal Knowledge Base Answer:")
        st.markdown(entry.get("faiss_summary", "_No summary available._"))

        # === Show Associated Images from Top Doc ===
        faiss_images = entry.get("faiss_images", [])
        faiss_sources = entry.get("faiss_sources", [])
        if faiss_images and faiss_sources:
            top_doc = faiss_sources[0][0]
            st.subheader(f"🖼️ Images from: {top_doc}")
            for meta in faiss_images:
                if meta.get("original_doc") == top_doc:
                    img_path = meta.get("extracted_image_path")
                    if img_path and os.path.exists(img_path):
                        st.image(img_path, caption=meta.get("caption", ""), use_container_width=True)

        # === Show Document Sources with Download Buttons ===
        st.subheader("📄 Document Sources:")
        for i, (docname, snippet, path) in enumerate(faiss_sources, 1):
            col1, col2 = st.columns([0.85, 0.15])
            with col1:
                st.markdown(f"**{i}. {docname}**\n\n{snippet}")
            with col2:
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        st.download_button(
                            label="⬇️",
                            data=f,
                            file_name=os.path.basename(path),
                            key=f"download_history_{i}"
                        )

    elif entry["route"] == "search":
        if entry.get("general_summary"):
            st.subheader("🧠 General Summary:")
            st.markdown(entry["general_summary"])

        st.subheader("🔗 Top Web Links:")
        for i, (link, summary) in enumerate(entry["result"], 1):
            st.markdown(f"**{i}.** {link}")
            st.markdown(f"_Summary:_\n{summary}")

    elif entry["route"] == "comp":
        # ✅ Show SQL Query
        if entry.get("sql_query"):
            st.subheader("🧾 SQL Query:")
            st.code(entry["sql_query"], language="sql")

        # ✅ Show SQL Result
        st.subheader("SQL Query Result:")
        result_df = entry.get("result")
        if isinstance(result_df, list):  # was serialized
            result_df = pd.DataFrame(result_df)
        if isinstance(result_df, pd.DataFrame):
            formatted_df = result_df.copy()
            for col in formatted_df.select_dtypes(include='number').columns:
                col_lower = col.lower()
                if "ratio" in col_lower:
                    formatted_df[col] = formatted_df[col].apply(lambda x: f"{x * 100:.2f}%" if pd.notnull(x) else "")
                elif any(keyword in col_lower for keyword in money_keywords):
                    formatted_df[col] = formatted_df[col].apply(lambda x: f"{x:,.0f}")
            st.dataframe(formatted_df)
        else:
            st.text(result_df)

        # ✅ Comparison Summary
        if entry.get("comparison_summary"):
            st.subheader("🆚 Comparison Summary:")
            st.markdown(entry["comparison_summary"])

        # ✅ General Summary
        if entry.get("general_summary"):
            st.subheader("🧠 General Summary:")
            st.markdown(entry["general_summary"])

        # ✅ Web Links
        st.subheader("🔗 Top Web Links:")
        web_links = entry.get("web_links")
        for i, (link, summary) in enumerate(web_links or [], 1):
            st.markdown(f"**{i}.** {link}")
            st.markdown(f"_Summary:_\n{summary}")

    ppt_buffer = generate_ppt(entry)
    st.download_button("⬇️ Export to PPT", ppt_buffer, file_name="agentic_ai_output.pptx")








