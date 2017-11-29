# @author rmitsch
# @date 2017-10-27
#

from flask import Flask
from flask import render_template
from flask import request
import backend.utils.Utils as Utils
from backend.algorithm.WordVector import WordVector
from backend.algorithm.QVEC import QVECConfiguration
from backend.algorithm.TSNEModel import TSNEModel
import werkzeug
from flask import jsonify
from backend.algorithm.WordEmbeddingClusterer import WordEmbeddingClusterer
import coranking
from coranking.metrics import trustworthiness, continuity
import numpy
import sklearn.neighbors
import json


def init_flask_app():
    """
    Initialize Flask app.
    :return: App object.
    """
    app = Flask(__name__,
                template_folder='frontend/templates',
                static_folder='frontend/static')
    # Limit of 100 MB for upload.
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

    return app


def fetch_word_embedding_for_dataset(app, dataset_name, invalidate_cache=False):
    """
    Fetches word embedding from database. Uses cached version, if available and cache invalidation not forced.
    :param app:
    :param dataset_name:
    :param invalidate_cache:
    :return:
    """
    word_embedding_dict = app.config["DATA"]["word_embeddings"]

    # Fetch word embedding from DB, if not done yet.
    if dataset_name not in word_embedding_dict or invalidate_cache:
        word_embedding_dict[dataset_name] = app.config["DB_CONNECTOR"].read_word_vectors_for_dataset(dataset_name)

    return word_embedding_dict[dataset_name]

# Initialize logger.
logger = Utils.create_logger()
# Initialize flask app.
app = init_flask_app()
# Connect to database.
app.config["DB_CONNECTOR"] = Utils.connect_to_database(False)
# Define version.
app.config["VERSION"] = "0.1"
# Define container for repeatedly used data objects.
app.config["DATA"] = {}
app.config["DATA"]["word_embeddings"] = {}


# root: Render HTML for start menu.
@app.route("/")
def index():
    return render_template("index.html", version=app.config["VERSION"], entrypoint="about")


@app.route('/', defaults={'path': ''}, methods=['POST'])
@app.route('/<path:path>', methods=['POST'])
def upload(path):
    """
    Constant memory-parsing of (word vector) files.
    :param path:
    :return:
    """

    # Next:
    #   - Create class for word vectors, move transformation code there.
    #   - Insert word vectors into DB.
    #   - Integrate DB with initial parameter histograms.
    #   - Construct dashboard layout.

    file_chunk = None

    # Parse data.
    stream, form, files = werkzeug.formparser.parse_form_data(
        request.environ,
        stream_factory=Utils.generate_custom_stream_factory
    )
    # Retrieve reference to sent file from generator.
    for file_item in files.values():
        file_chunk = file_item

    # If first chunk (i. e. user is uploading a new dataset): Determine number of dimensions.
    if int(request.args['resumableChunkNumber']) == 1:
        WordVector.extract_number_of_dimensions(file_chunk)

    # Create entry in datasets, if it doesn't exist yet. Otherwise just fetch ID.
    dataset_id = app.config["DB_CONNECTOR"].import_dataset(
        request.args['resumableFilename'],
        WordVector.num_dimensions
    )

    # Extract content from file.
    tuples_to_insert = WordVector.extract_word_vectors_from_file_chunk(
        file_chunk=file_chunk,
        chunk_number=int(request.args['resumableChunkNumber']),
        dataset_id=dataset_id)

    # Insert vectors in this chunk into DB.
    # app.config["DB_CONNECTOR"].import_word_vectors(tuples_to_insert)

    return "200"


@app.route('/carousel_content', methods=["GET", "POST"])
def fetch_carousel():
    return app.send_static_file('index_carousel.html')


@app.route('/dashboard_content', methods=["GET"])
def fetch_dashboard():
    return app.send_static_file('dashboard.html')


@app.route('/dataset_metadata', methods=["GET", "POST"])
def fetch_dataset_metadata():
    return jsonify(app.config["DB_CONNECTOR"].read_first_run_metadata())


@app.route('/dataset_word_counts', methods=["GET"])
def fetch_dataset_word_counts():
    return jsonify(app.config["DB_CONNECTOR"].read_dataset_word_counts())


@app.route('/create_new_run', methods=["POST"])
def create_new_run():
    # Insert new run into DB.
    app.config["DB_CONNECTOR"].insert_new_run(request.get_json())

    return "200"


@app.route('/runs', methods=["GET"])
def read_runs_for_dataset():
    return jsonify(app.config["DB_CONNECTOR"].read_runs_for_dataset(dataset_name=request.args["dataset_name"]))


@app.route('/check_we_model', methods=["POST"])
def determine_wordembedding_accuracy():
    """
    Determines WE accuracy. Stores result in DB.
    :return: 200 if successful. 500 if failing.
    """
    dataset_name = request.get_json()

    # Fetch word embedding from DB.
    fetch_word_embedding_for_dataset(app=app, dataset_name=dataset_name, invalidate_cache=False)

    qvec = QVECConfiguration()
    app.config["DB_CONNECTOR"].set_dataset_qvec_score(
        dataset_name=dataset_name,
        qvec_score=qvec.run(word_embedding=app.config["DATA"]["word_embeddings"][dataset_name])
    )
    return "200"


@app.route('/cluster_we_model', methods=["POST"])
def cluster_wordembedding():
    """
    Clusters original WE using HDBSCAN.
    :return: 200 if successful. 500 if failing.
    """
    dataset_name = request.get_json()

    # Fetch word embedding from DB, if not done yet.
    fetch_word_embedding_for_dataset(app=app, dataset_name=dataset_name, invalidate_cache=False)

    # Prepare and cluster data.
    we_clusterer = WordEmbeddingClusterer(
        WordEmbeddingClusterer.prepare_word_embedding_dataset(
            WordEmbeddingClusterer(app.config["DATA"]["word_embeddings"][dataset_name])
        )
    )
    app.config["DATA"]["word_embeddings"][dataset_name]["cluster_id"] = we_clusterer.run()

    # Update word vector's cluster ID in DB.
    app.config["DB_CONNECTOR"].update_word_vectors_cluster_ids(
        word_embedding=app.config["DATA"]["word_embeddings"][dataset_name]
    )

    return "200"


@app.route('/create_initial_tsne_model', methods=["POST"])
def create_initial_tsne_model():
    """
    Calculates initial t-SNE model for newly created run.
    :return:
    """
    initial_tsne_parameters = request.get_json()
    dataset_name = initial_tsne_parameters["dataset"]
    num_words_to_use = initial_tsne_parameters["numWords"]

    # Fetch word embedding from DB, if not done yet.
    fetch_word_embedding_for_dataset(app=app, dataset_name=dataset_name, invalidate_cache=False)

    # 1. Insert metadata for initial t-SNE model.
    tsne_model_id = app.config["DB_CONNECTOR"].insert_tsne_model(
        tsne_configuration=initial_tsne_parameters,
        sequence_number_in_run=1
    )

    # 2. If t-SNE model doesn't exist yet: Generate, persist.
    if tsne_model_id != -1:
        limited_word_embedding = app.config["DATA"]["word_embeddings"][dataset_name].head(num_words_to_use)

        # Calculate t-SNE model.
        initial_tsne_model = TSNEModel.generate_instance_from_dict(initial_tsne_parameters)
        tsne_results = initial_tsne_model.run(word_embedding=limited_word_embedding)

        # Calculate and persist initial, clustered t-SNE model.
        app.config["DB_CONNECTOR"].insert_tsne_coordinates(
            tsne_model_id=tsne_model_id,
            word_ids=limited_word_embedding['id'].values,
            cluster_ids=WordEmbeddingClusterer(tsne_results).run(),
            tsne_result=tsne_results
        )

    return str(tsne_model_id)


@app.route('/calculate_quality_measures', methods=["POST"])
def calculate_quality_measures():
    """
    Calculates quality measures (QVEC, trustworthiness, continuity, generalization accuracy) for t-SNE model.
    :return:
    """
    dataset_name = request.get_json()["dataset_name"]
    tsne_model_id = request.get_json()["tsne_model_id"]
    num_words_to_use = request.get_json()["num_words"]

    # 0. Fetch word embedding from DB, if not done yet.
    fetch_word_embedding_for_dataset(app=app, dataset_name=dataset_name, invalidate_cache=False)
    limited_word_embedding = app.config["DATA"]["word_embeddings"][dataset_name].head(num_words_to_use)

    # 1. Load t-SNE results.
    tsne_results = app.config["DB_CONNECTOR"].read_tsne_results(tsne_model_id=tsne_model_id)

    # 2. Calculate QVEC score.
    qvec_score = QVECConfiguration().run(word_embedding=tsne_results)

    # 3. Calculate coranking matrix.
    limited_word_embedding_vector_array = numpy.stack(limited_word_embedding["values"].values, axis=0)
    tsne_model_vector_array = numpy.stack(tsne_results["values"].values, axis=0)
    coranking_matrix = coranking.coranking_matrix(limited_word_embedding_vector_array, tsne_model_vector_array)

    # 4. Calculate trustworthiness.
    trust = trustworthiness(coranking_matrix.astype(numpy.float16), min_k=99, max_k=100)

    # 5. Calculate continuity.
    cont = continuity(coranking_matrix.astype(numpy.float16), min_k=99, max_k=100)

    # 6. Calculate generalization accuracy.
    # Use nearest neighbour search with ball tree to find nearest neighbour in t-SNE vector space.
    neighbours = sklearn.neighbors.NearestNeighbors(n_neighbors=2, algorithm='ball_tree').fit(tsne_model_vector_array)
    distances, indices = neighbours.kneighbors(tsne_model_vector_array)
    # Classify points using the cluster ID's of their nearest neighbours, compare with their actual cluster ID to
    # calculate generalization accuracy.
    predicted_cluster_values = limited_word_embedding.iloc[indices[:, 1]]["cluster_id"].values
    actual_cluster_values = limited_word_embedding["cluster_id"].values
    generalization_accuracy = numpy.sum(predicted_cluster_values == actual_cluster_values) / num_words_to_use

    # 7. Store results in DB.
    app.config["DB_CONNECTOR"].set_tsne_quality_scores(
        trustworthiness=float(trust[0]),
        continuity=float(cont[0]),
        generalization_accuracy=generalization_accuracy,
        qvec_score=qvec_score,
        tsne_id=tsne_model_id
    )

    return "200"


@app.route('/fetch_latest_tsne_results_for_run', methods=["GET"])
def fetch_latest_tsne_results_for_run():
    """
    Fetches t-SNE results for latest model in specified run.
    :return:
    """
    run_title = request.args["run_title"]
    tsne_results = app.config["DB_CONNECTOR"].read_tsne_results_for_latest_tsne_model_in_run(run_title=run_title)

    return jsonify(tsne_results.to_json(orient="index"))


@app.route('/fetch_model_metadata_in_runs_in_dataset', methods=["GET"])
def fetch_model_metadata_in_runs_in_dataset():
    """
    Fetches metadata for all models in all runs in specified dataset.
    :return:
    """
    return jsonify(app.config["DB_CONNECTOR"].read_metadata_for_run(run_title=request.args["run_title"]))


@app.route('/proceed_with_optimization', methods=["POST"])
def proceed_with_optimization():
    """
    Proceeds with optimization of current t-SNE model.
    :return: Result of latest t-SNE model.
    """

    print(request.get_json())

    return "200"



if __name__ == "__main__":
    app.run(host='0.0.0.0', port=7182, debug=True)
