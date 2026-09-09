#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <atomic>
#include <cmath>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include "RNA_class/RNA.h"
#include "RNA_class/thermodynamics.h"
#include "src/bimol.h"
#include "src/defines.h"
#include "src/structure.h"
#include "src/TProgressDialog.h"

namespace {

constexpr const char* kVersion = "6.6";
constexpr const char* kTokenName = "mbuprime.rnastructure.cancel.v1";

using CancelState = std::shared_ptr<std::atomic_bool>;

struct NativeStructure {
    std::vector<std::pair<int, int>> pairs;
    double search_dg = 0.0;
};

struct FixedScore {
    double dg37 = 0.0;
    double dh = 0.0;
    double requested = 0.0;
};

class AtomicProgress final : public ProgressHandler {
public:
    explicit AtomicProgress(CancelState state) : state_(std::move(state)) {}
    bool canceled() const override {
        return state_ && state_->load(std::memory_order_relaxed);
    }
    void cancel() override {
        if (state_) state_->store(true, std::memory_order_relaxed);
    }
private:
    CancelState state_;
};

void delete_cancel_token(PyObject* capsule) {
    void* raw = PyCapsule_GetPointer(capsule, kTokenName);
    if (raw != nullptr) delete static_cast<CancelState*>(raw);
    else PyErr_Clear();
}

CancelState cancel_state(PyObject* capsule) {
    if (capsule == nullptr || capsule == Py_None) return {};
    void* raw = PyCapsule_GetPointer(capsule, kTokenName);
    if (raw == nullptr) throw std::invalid_argument("invalid cancellation token");
    return *static_cast<CancelState*>(raw);
}

void throw_if_cancelled(const CancelState& state) {
    if (state && state->load(std::memory_order_relaxed)) {
        throw std::runtime_error("native calculation cancelled");
    }
}

struct ThermoCacheEntry {
    std::string directory;
    double temperature_k;
    std::shared_ptr<Thermodynamics> tables;
    std::uint64_t last_used;
};

std::shared_ptr<Thermodynamics> load_tables(
        const char* directory, double temperature_k) {
    constexpr size_t kMaximumCachedTables = 8;
    static std::mutex mutex;
    static std::vector<ThermoCacheEntry> cache;
    static std::uint64_t use_counter = 0;
    std::lock_guard<std::mutex> lock(mutex);
    ++use_counter;
    for (ThermoCacheEntry& entry : cache) {
        if (entry.directory == directory &&
                entry.temperature_k == temperature_k) {
            entry.last_used = use_counter;
            return entry.tables;
        }
    }

    // RNAstructure's copy constructor deliberately shares immutable loaded
    // tables for repeated calculations. Keep a bounded set alive so every
    // fold/score does not reread the same parameter files from disk.
    auto tables = std::make_shared<Thermodynamics>(
        false, "dna", temperature_k);
    const int error = tables->ReadThermodynamic(directory, "dna", temperature_k);
    if (error != 0 || !tables->GetEnergyRead()) {
        throw std::runtime_error(
            "RNAstructure could not load explicit DNA tables (error " +
            std::to_string(error) + ")");
    }
    if (cache.size() == kMaximumCachedTables) {
        auto oldest = cache.begin();
        for (auto item = cache.begin() + 1; item != cache.end(); ++item) {
            if (item->last_used < oldest->last_used) oldest = item;
        }
        cache.erase(oldest);
    }
    cache.push_back({directory, temperature_k, tables, use_counter});
    return tables;
}

std::vector<std::pair<int, int>> hairpin_pairs(RNA& rna, int structure_number) {
    std::vector<std::pair<int, int>> result;
    const int length = rna.GetSequenceLength();
    for (int i = 1; i <= length; ++i) {
        const int partner = rna.GetPair(i, structure_number);
        if (partner > i) result.emplace_back(i - 1, partner - 1);
    }
    return result;
}

std::vector<std::pair<int, int>> duplex_pairs(
        structure& combined, int structure_number, int first_length) {
    std::vector<std::pair<int, int>> result;
    const int second_start = first_length + 4; // RNAstructure is one-based; III linker.
    for (int i = 1; i <= first_length; ++i) {
        const int partner = combined.GetPair(i, structure_number);
        if (partner >= second_start) {
            result.emplace_back(i - 1, partner - second_start);
        }
    }
    return result;
}

PyObject* structures_to_python(const std::vector<NativeStructure>& structures) {
    PyObject* output = PyTuple_New(static_cast<Py_ssize_t>(structures.size()));
    if (output == nullptr) return nullptr;
    for (Py_ssize_t index = 0; index < static_cast<Py_ssize_t>(structures.size()); ++index) {
        const NativeStructure& item = structures[static_cast<size_t>(index)];
        PyObject* pairs = PyTuple_New(static_cast<Py_ssize_t>(item.pairs.size()));
        if (pairs == nullptr) { Py_DECREF(output); return nullptr; }
        for (Py_ssize_t pair_index = 0;
             pair_index < static_cast<Py_ssize_t>(item.pairs.size()); ++pair_index) {
            const auto& pair = item.pairs[static_cast<size_t>(pair_index)];
            PyObject* value = Py_BuildValue("(ii)", pair.first, pair.second);
            if (value == nullptr) { Py_DECREF(pairs); Py_DECREF(output); return nullptr; }
            PyTuple_SET_ITEM(pairs, pair_index, value);
        }
        PyObject* record = Py_BuildValue(
            "{s:N,s:d}", "pairs", pairs,
            "search_dg_kcal_mol", item.search_dg);
        if (record == nullptr) { Py_DECREF(output); return nullptr; }
        PyTuple_SET_ITEM(output, index, record);
    }
    return output;
}

PyObject* scores_to_python(const std::vector<FixedScore>& scores) {
    PyObject* output = PyTuple_New(static_cast<Py_ssize_t>(scores.size()));
    if (output == nullptr) return nullptr;
    for (Py_ssize_t index = 0; index < static_cast<Py_ssize_t>(scores.size()); ++index) {
        const FixedScore& item = scores[static_cast<size_t>(index)];
        PyObject* record = Py_BuildValue(
            "{s:d,s:d,s:d}",
            "dg_37_kcal_mol", item.dg37,
            "dh_kcal_mol", item.dh,
            "dg_requested_kcal_mol", item.requested);
        if (record == nullptr) { Py_DECREF(output); return nullptr; }
        PyTuple_SET_ITEM(output, index, record);
    }
    return output;
}

void set_native_error(const std::exception& error) {
    const std::string message = error.what();
    if (message.find("cancelled") != std::string::npos) {
        PyErr_SetString(PyExc_InterruptedError, message.c_str());
    } else {
        PyErr_SetString(PyExc_RuntimeError, message.c_str());
    }
}

PyObject* py_make_cancel_token(PyObject*, PyObject*) {
    auto* state = new CancelState(std::make_shared<std::atomic_bool>(false));
    PyObject* capsule = PyCapsule_New(state, kTokenName, delete_cancel_token);
    if (capsule == nullptr) delete state;
    return capsule;
}

PyObject* py_cancel(PyObject*, PyObject* args) {
    PyObject* capsule = nullptr;
    if (!PyArg_ParseTuple(args, "O", &capsule)) return nullptr;
    try {
        CancelState state = cancel_state(capsule);
        if (state) state->store(true, std::memory_order_relaxed);
        Py_RETURN_NONE;
    } catch (const std::exception& error) {
        set_native_error(error);
        return nullptr;
    }
}

PyObject* py_fold_hairpin(PyObject*, PyObject* args, PyObject* kwargs) {
    const char* sequence = nullptr;
    const char* table_dir = nullptr;
    double temperature_k = 0.0;
    int maximum = 0;
    int window = 0;
    PyObject* token = Py_None;
    static const char* names[] = {
        "sequence", "table_dir", "temperature_k", "maximum_structures",
        "window", "cancel_token", nullptr};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "ssdii|O", const_cast<char**>(names),
            &sequence, &table_dir, &temperature_k, &maximum, &window, &token)) {
        return nullptr;
    }
    try {
        CancelState state = cancel_state(token);
        std::vector<NativeStructure> output;
        std::exception_ptr native_error;
        Py_BEGIN_ALLOW_THREADS
        try {
            throw_if_cancelled(state);
            auto thermo = load_tables(table_dir, temperature_k);
            RNA rna(sequence, SEQUENCE_STRING, thermo.get());
            const int constructor_error = rna.GetErrorCode();
            if (constructor_error != 0) {
                throw std::runtime_error(rna.GetErrorMessage(constructor_error));
            }
            AtomicProgress progress(state);
            rna.SetProgress(progress);
            // Exact Fold CLI parity: --percent 10, --maximum N, automatic
            // window, --maxLoop 30, simple internal loops, coaxial allowed,
            // and isolated pairs forbidden.
            const int error = rna.FoldSingleStrand(
                10.0f, maximum, window, "", 30, false, true, false, false);
            rna.StopProgress();
            if (progress.canceled() || error == 99) {
                throw std::runtime_error("native calculation cancelled");
            }
            if (error != 0) throw std::runtime_error(rna.GetErrorMessage(error));
            const int count = rna.GetStructureNumber();
            output.reserve(static_cast<size_t>(count));
            for (int index = 1; index <= count; ++index) {
                output.push_back({hairpin_pairs(rna, index), rna.GetFreeEnergy(index)});
            }
        } catch (...) { native_error = std::current_exception(); }
        Py_END_ALLOW_THREADS
        if (native_error) std::rethrow_exception(native_error);
        return structures_to_python(output);
    } catch (const std::exception& error) {
        set_native_error(error);
        return nullptr;
    }
}

PyObject* py_fold_duplex(PyObject*, PyObject* args, PyObject* kwargs) {
    const char* sequence_a = nullptr;
    const char* sequence_b = nullptr;
    const char* table_dir = nullptr;
    double temperature_k = 0.0;
    int maximum = 0;
    PyObject* token = Py_None;
    static const char* names[] = {
        "sequence_a", "sequence_b", "table_dir", "temperature_k",
        "maximum_structures", "cancel_token", nullptr};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "sssdi|O", const_cast<char**>(names),
            &sequence_a, &sequence_b, &table_dir, &temperature_k,
            &maximum, &token)) return nullptr;
    try {
        CancelState state = cancel_state(token);
        std::vector<NativeStructure> output;
        std::exception_ptr native_error;
        Py_BEGIN_ALLOW_THREADS
        try {
            throw_if_cancelled(state);
            auto thermo = load_tables(table_dir, temperature_k);
            RNA first(sequence_a, SEQUENCE_STRING, thermo.get());
            RNA second(sequence_b, SEQUENCE_STRING, thermo.get());
            if (first.GetErrorCode() != 0 || second.GetErrorCode() != 0) {
                throw std::runtime_error("RNAstructure could not load duplex sequence");
            }
            structure combined;
            // Exact DuplexFold CLI parity: --percent 40, --maximum N,
            // window 0, and max internal loop 6.
            // This is the implementation used by HybridRNA::FoldDuplex, but
            // uses the already explicitly loaded table rather than DATAPATH.
            bimol(first.GetStructure(), second.GetStructure(), &combined,
                  6, maximum, 40, 0, thermo->GetDatatable());
            combined.intermolecular = true;
            const int first_length = first.GetSequenceLength();
            combined.inter[0] = first_length + 1;
            combined.inter[1] = first_length + 2;
            combined.inter[2] = first_length + 3;
            throw_if_cancelled(state); // upstream bimol has no progress hook.
            const int count = combined.GetNumberofStructures();
            output.reserve(static_cast<size_t>(count));
            for (int index = 1; index <= count; ++index) {
                output.push_back({
                    duplex_pairs(combined, index, first_length),
                    static_cast<double>(combined.GetEnergy(index)) /
                        conversionfactor});
            }
        } catch (...) { native_error = std::current_exception(); }
        Py_END_ALLOW_THREADS
        if (native_error) std::rethrow_exception(native_error);
        return structures_to_python(output);
    } catch (const std::exception& error) {
        set_native_error(error);
        return nullptr;
    }
}

std::vector<double> calculate_fixed(
        const char* ct_path, const char* table_dir, double temperature_k,
        const CancelState& state,
        const std::vector<NativeStructure>* geometries = nullptr,
        int linker_start = 0) {
    throw_if_cancelled(state);
    auto thermo = load_tables(table_dir, temperature_k);
    // In-memory batches use the same sequence, III linker and one-based pairs
    // as openct(), without crossing Windows' narrow filename boundary.
    RNA rna(ct_path, geometries ? SEQUENCE_STRING : FILE_CT, thermo.get());
    const int error = rna.GetErrorCode();
    if (error != 0) throw std::runtime_error(rna.GetErrorMessage(error));
    if (geometries) {
        structure* ct = rna.GetStructure();
        if (linker_start) {
            ct->intermolecular = true;
            for (int index = 0; index < 3; ++index)
                ct->inter[index] = linker_start + index;
        }
        for (const NativeStructure& geometry : *geometries) {
            throw_if_cancelled(state);
            ct->AddStructure();
            for (const auto& pair : geometry.pairs)
                ct->SetPair(pair.first + 1, pair.second + 1,
                            ct->GetNumberofStructures());
        }
    }
    std::vector<double> values;
    const int count = rna.GetStructureNumber();
    values.reserve(static_cast<size_t>(count));
    for (int index = 1; index <= count; ++index) {
        throw_if_cancelled(state);
        values.push_back(rna.CalculateFreeEnergy(index, true));
    }
    return values;
}

PyObject* py_score_ct(PyObject*, PyObject* args, PyObject* kwargs) {
    const char* ct_path = nullptr;
    const char* scaled_dir = nullptr;
    const char* enthalpy_dir = nullptr;
    double requested_temperature_k = 0.0;
    PyObject* token = Py_None;
    PyObject* pair_batches = Py_None;
    int linker_start = 0;
    static const char* names[] = {
        "ct_path", "scaled_table_dir", "enthalpy_table_dir",
        "requested_temperature_k", "cancel_token", "pair_batches",
        "linker_start", nullptr};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "sssd|OOi", const_cast<char**>(names),
            &ct_path, &scaled_dir, &enthalpy_dir,
            &requested_temperature_k, &token, &pair_batches,
            &linker_start)) return nullptr;
    try {
        CancelState state = cancel_state(token);
        std::vector<NativeStructure> geometries;
        const auto* batch = pair_batches == Py_None ? nullptr : &geometries;
        if (batch) {
            const std::string sequence(ct_path);
            const int length = static_cast<int>(sequence.size());
            if (length < 1 || length > 20000 || linker_start < 0 ||
                    (linker_start && (linker_start < 2 || linker_start + 2 >= length)))
                throw std::invalid_argument("invalid fixed-geometry sequence or linker");
            for (int index = 0; index < length; ++index) {
                const bool linker = linker_start && index >= linker_start - 1 &&
                    index < linker_start + 2;
                if (linker ? sequence[index] != 'I' :
                        std::string("ACGT").find(sequence[index]) == std::string::npos)
                    throw std::invalid_argument("invalid fixed-geometry nucleotide");
            }
            if (!PyTuple_Check(pair_batches))
                throw std::invalid_argument("fixed-geometry batches must be a tuple");
            for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(pair_batches); ++index) {
                PyObject* pairs = PyTuple_GET_ITEM(pair_batches, index);
                if (!PyTuple_Check(pairs))
                    throw std::invalid_argument("fixed-geometry pairs must be a tuple");
                NativeStructure geometry;
                std::vector<bool> paired(length, false);
                for (Py_ssize_t pair_index = 0; pair_index < PyTuple_GET_SIZE(pairs); ++pair_index) {
                    PyObject* pair = PyTuple_GET_ITEM(pairs, pair_index);
                    int left, right;
                    if (!PyArg_ParseTuple(pair, "ii", &left, &right)) return nullptr;
                    if (left < 0 || right <= left || right >= length ||
                            paired[left] || paired[right] ||
                            sequence[left] == 'I' || sequence[right] == 'I')
                        throw std::invalid_argument("invalid fixed-geometry pair");
                    paired[left] = paired[right] = true;
                    geometry.pairs.emplace_back(left, right);
                }
                geometries.push_back(std::move(geometry));
            }
        }
        std::vector<FixedScore> output;
        std::exception_ptr native_error;
        Py_BEGIN_ALLOW_THREADS
        try {
            // CalculateFreeEnergy delegates to efn2(simple=True) and exactly
            // matches the CLI scorer while keeping every call in-process.
            std::vector<double> dg37 = calculate_fixed(
                ct_path, scaled_dir, 310.15, state, batch, linker_start);
            std::vector<double> dh = calculate_fixed(
                ct_path, enthalpy_dir, 310.15, state, batch, linker_start);
            throw_if_cancelled(state);
            // At exactly 37 C the CT, tables and temperature are identical
            // to the completed G37 pass; reuse its exact scores.
            std::vector<double> requested = requested_temperature_k == 310.15
                ? dg37 : calculate_fixed(
                    ct_path, scaled_dir, requested_temperature_k, state, batch, linker_start);
            if (dg37.size() != dh.size() || dg37.size() != requested.size()) {
                throw std::runtime_error("fixed-geometry score count mismatch");
            }
            output.reserve(dg37.size());
            for (size_t index = 0; index < dg37.size(); ++index) {
                output.push_back({dg37[index], dh[index], requested[index]});
            }
        } catch (...) { native_error = std::current_exception(); }
        Py_END_ALLOW_THREADS
        if (native_error) std::rethrow_exception(native_error);
        return scores_to_python(output);
    } catch (const std::exception& error) {
        set_native_error(error);
        return nullptr;
    }
}

PyObject* py_build_info(PyObject*, PyObject*) {
    return Py_BuildValue(
        "{s:s,s:s,s:s}",
        "engine_version", kVersion,
        "integration", "in_process_native",
        "cancellation", "hairpin cooperative; duplex phase-boundary");
}

PyMethodDef methods[] = {
    {"make_cancel_token", py_make_cancel_token, METH_NOARGS, nullptr},
    {"cancel", py_cancel, METH_VARARGS, nullptr},
    {"fold_hairpin", reinterpret_cast<PyCFunction>(py_fold_hairpin),
     METH_VARARGS | METH_KEYWORDS, nullptr},
    {"fold_duplex", reinterpret_cast<PyCFunction>(py_fold_duplex),
     METH_VARARGS | METH_KEYWORDS, nullptr},
    {"score_ct", reinterpret_cast<PyCFunction>(py_score_ct),
     METH_VARARGS | METH_KEYWORDS, nullptr},
    {"build_info", py_build_info, METH_NOARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_rnastructure_native_v1",
    "Narrow in-process RNAstructure 6.6 DNA folding/scoring boundary.",
    -1,
    methods,
};

} // namespace

PyMODINIT_FUNC PyInit__rnastructure_native_v1() {
    return PyModule_Create(&module);
}
