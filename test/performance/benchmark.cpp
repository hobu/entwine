// A single benchmark sample per process, driven by run.py.
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>

#if defined(__linux__) || defined(__APPLE__)
#include <sys/resource.h>
#endif

#include <pdal/PipelineManager.hpp>
#include <pdal/io/BufferReader.hpp>
#include <pdal/io/LasWriter.hpp>
#include <pdal/pdal_config.hpp>

#include <entwine/builder/builder.hpp>
#include <entwine/io/binary.hpp>
#include <entwine/util/config.hpp>
#include <entwine/util/info.hpp>

using namespace entwine;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

namespace
{
double seconds(Clock::time_point start)
{
    return std::chrono::duration<double>(Clock::now() - start).count();
}

json readJson(const fs::path& path)
{
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("Cannot read " + path.string());
    return json::parse(stream);
}

void check(bool condition, const std::string& message)
{
    if (!condition) throw std::runtime_error(message);
}

// Snapshot before readback validation: RSS is a process high-water mark, so
// generation and other samples must run in separate processes.
json resources()
{
#if defined(__linux__) || defined(__APPLE__)
    rusage usage{};
    check(getrusage(RUSAGE_SELF, &usage) == 0, "getrusage failed");
    const double cpu = usage.ru_utime.tv_sec + usage.ru_stime.tv_sec +
        (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e6;
    uint64_t rss = usage.ru_maxrss;
#ifndef __APPLE__
    rss *= 1024; // Linux reports KiB; macOS reports bytes.
#endif
    return {{"cpu_seconds", cpu}, {"peak_rss_bytes", rss}};
#else
    return {{"cpu_seconds", nullptr}, {"peak_rss_bytes", nullptr}};
#endif
}

uint64_t checkSources(const SourceList& sources)
{
    check(!sources.empty(), "No input sources");
    uint64_t points = 0;
    for (const auto& source : sources)
    {
        check(source.info.errors.empty(), "Input error: " + source.path +
            " " + json(source.info.errors).dump());
        points += source.info.points;
    }
    check(points > 0, "No input points");
    return points;
}

json generate(const json& request)
{
    const fs::path dir = request.at("output").get<std::string>();
    check(!fs::exists(dir), "Generation requires a new output directory");
    fs::create_directories(dir);
    const uint64_t count = request.at("points");
    const unsigned files = request.at("files");
    check(files && count >= files, "Need at least one point per file");
    const std::string distribution = request.at("distribution");
    const std::string format = request.at("input_format");
    check(distribution == "uniform" || distribution == "clustered",
        "Unknown distribution");
    check(format == "las" || format == "laz", "Unknown input format");
    std::mt19937_64 rng(request.at("seed").get<uint64_t>());
    // Explicit conversion avoids implementation-dependent real distributions.
    const auto random = [&]() { return (rng() >> 11) * 0x1.0p-53; };
    StringList inputs;
    for (unsigned file = 0; file < files; ++file)
    {
        pdal::PointTable table;
        table.layout()->registerDims({DimId::X, DimId::Y, DimId::Z,
            DimId::Intensity, DimId::Classification, DimId::GpsTime,
            DimId::Red, DimId::Green, DimId::Blue});
        auto view = std::make_shared<pdal::PointView>(table);
        const uint64_t n = count / files + (file < count % files);
        for (uint64_t i = 0; i < n; ++i)
        {
            double x = random(), y = random(), z = random();
            if (distribution == "clustered")
            {
                const unsigned cluster = rng() % 8;
                x = 0.1 + (cluster & 1 ? 0.7 : 0) + x * 0.05;
                y = 0.1 + (cluster & 2 ? 0.7 : 0) + y * 0.05;
                z = 0.1 + (cluster & 4 ? 0.7 : 0) + z * 0.05;
            }
            view->setField(DimId::X, i, -8243000 + x * 1000);
            view->setField(DimId::Y, i, 4966000 + y * 1000);
            view->setField(DimId::Z, i, z * 200);
            view->setField(DimId::Intensity, i, rng() % 65536);
            view->setField(DimId::Classification, i, 1 + rng() % 5);
            // Unsorted GPS times exercise the LASzip writer's sorting path.
            view->setField(DimId::GpsTime, i, random() * 1e6);
            view->setField(DimId::Red, i, rng() % 65536);
            view->setField(DimId::Green, i, rng() % 65536);
            view->setField(DimId::Blue, i, rng() % 65536);
        }
        const std::string path = (dir / ("tile-" + std::to_string(file) +
            "." + format)).string();
        pdal::BufferReader reader;
        reader.addView(view);
        pdal::Options options;
        options.add("filename", path);
        options.add("minor_version", 2);
        options.add("dataformat_id", 3);
        options.add("creation_year", 2020);
        options.add("creation_doy", 1);
        options.add("a_srs", "EPSG:3857");
        options.add("scale_x", 0.001);
        options.add("scale_y", 0.001);
        options.add("scale_z", 0.001);
        options.add("offset_x", -8243000);
        options.add("offset_y", 4966000);
        pdal::LasWriter writer;
        writer.setInput(reader);
        writer.setOptions(options);
        writer.prepare(table);
        writer.execute(table);
        inputs.push_back(path);
    }
    return {{"input", inputs}, {"points", count}};
}

json sample(const json& request)
{
    const std::string workload = request.at("case");
    const StringList inputs = request.at("input").get<StringList>();
    const uint64_t expected = request.at("points");
    json result;
    json before = resources();
    if (workload == "pack-binary")
    {
        // Isolate serialization from tree construction. Fixture preparation is
        // outside both CPU and wall timers, but included in process peak RSS.
        const Schema schema = {
            Dimension("X", Type::Signed32, .25, 1000),
            Dimension("Y", Type::Signed32, .25, -2000),
            Dimension("Z", Type::Signed32, .25, 0),
            Dimension("Intensity", Type::Unsigned16),
            Dimension("Classification", Type::Unsigned8),
            Dimension("GpsTime", Type::Double),
            Dimension("Red", Type::Unsigned16),
            Dimension("Green", Type::Unsigned16),
            Dimension("Blue", Type::Unsigned16),
            Dimension("OriginId", Type::Unsigned32),
            Dimension("PointId", Type::Unsigned64)
        };
        const Metadata metadata(currentEptVersion(), schema, Bounds(), Bounds(),
            {}, {}, io::Type::Binary, 32, BuildParameters(1024, 4096));
        auto layout = toLayout(metadata.absoluteSchema, false);
        MemBlock block(layout.pointSize(), 4096);
        for (uint64_t i = 0; i < expected; ++i) block.next();
        BlockPointTable table(layout);
        table.insert(block);
        pdal::PointRef point(table);
        for (uint64_t i = 0; i < expected; ++i)
        {
            point.setPointId(i);
            point.setField(DimId::X, 1000 + (i % 2048) * .125);
            point.setField(DimId::Y, -2000 - (i % 2048) * .125);
            point.setField(DimId::Z, (i % 1024) * .125);
            point.setField(DimId::Intensity, i % 65536);
            point.setField(DimId::Classification, i % 32);
            point.setField(DimId::GpsTime, i * .125);
            point.setField(DimId::Red, i % 65536);
            point.setField(DimId::Green, (i + 1) % 65536);
            point.setField(DimId::Blue, (i + 2) % 65536);
            point.setField(layout.findDim("OriginId"), i % 8);
            point.setField(layout.findDim("PointId"), i);
        }
        before = resources();
        const auto start = Clock::now();
        const auto packed = io::binary::pack(metadata, table);
        result["wall_seconds"] = seconds(start);
        result["resources"] = resources();
        check(packed.size() == expected * getPointSize(schema), "Packed size mismatch");
        const fs::path output = request.at("output").get<std::string>();
        check(!fs::exists(output), "A sample requires a fresh output directory");
        fs::create_directories(output);
        std::ofstream stream(output / "packed.bin", std::ios::binary);
        stream.write(packed.data(), packed.size());
        check(stream.good(), "Cannot write packed output");
        result["output_bytes"] = packed.size();
    }
    else if (workload == "analyze-shallow" || workload == "analyze-deep")
    {
        const auto start = Clock::now();
        const auto sources = analyze(inputs, json::array({json::object()}),
            workload == "analyze-deep", request.at("tmp"), {},
            request.at("analysis_threads"), false);
        result["wall_seconds"] = seconds(start);
        result["resources"] = resources();
        check(checkSources(sources) == expected, "Analysis point count mismatch");
    }
    else
    {
        check(workload == "build-binary" || workload == "build-laszip" ||
            workload == "build-reproject", "Unknown benchmark case");
        const fs::path output = request.at("output").get<std::string>();
        check(!fs::exists(output), "A sample requires a fresh output directory");
        json config = {
            {"input", inputs}, {"output", output.string()},
            {"tmp", request.at("tmp")}, {"force", true},
            {"verbose", false}, {"progressInterval", 0},
            {"span", request.at("span")}, {"threads", request.at("analysis_threads")},
            {"dataType", workload == "build-laszip" ? "laszip" : "binary"}
        };
        if (workload == "build-reproject")
            config["reprojection"] = {{"out", "EPSG:26918"}};

        ChunkCache::latchInfo();
        const auto start = Clock::now();
        Builder builder = builder::create(config);
        const double createSeconds = seconds(start);
        // create() accepts a total analysis thread count; run() also accepts
        // explicit work/clip pools. Keep these distinct for controlled sweeps.
        config["threads"] = request.at("threads");
        const auto runStart = Clock::now();
        const uint64_t inserted = builder::run(builder, config);
        result["run_seconds"] = seconds(runStart);
        result["wall_seconds"] = seconds(start);
        result["create_seconds"] = createSeconds;
        result["resources"] = resources();
        const auto io = ChunkCache::latchInfo();
        result["nodes_written"] = io.written;
        result["nodes_read"] = io.read;
        check(inserted == expected, "Inserted point count mismatch");
        for (const auto& item : builder.manifest)
            check(item.source.info.errors.empty(), "Build reported an input error");
        const fs::path ept = output / "ept.json";
        check(readJson(ept).at("points").get<uint64_t>() == expected,
            "EPT metadata point count mismatch");

        // Decode every output node outside the measured region. A fast but
        // incomplete or unreadable build must never become a good sample.
        pdal::PipelineManager pm;
        std::istringstream pipeline(json::array({ept.string()}).dump());
        pm.readPipeline(pipeline);
        pm.execute();
        uint64_t readback = 0;
        for (const auto& view : pm.views()) readback += view->size();
        check(readback == expected, "EPT readback point count mismatch");
        uint64_t bytes = 0;
        for (const auto& entry : fs::recursive_directory_iterator(output))
            if (entry.is_regular_file()) bytes += entry.file_size();
        result["output_bytes"] = bytes;
    }
    const auto usage = result.at("resources");
    result.erase("resources");
    result["peak_rss_bytes"] = usage.at("peak_rss_bytes");
    result["cpu_seconds"] = usage.at("cpu_seconds").is_null() ? json(nullptr) :
        json(usage.at("cpu_seconds").get<double>() -
            before.at("cpu_seconds").get<double>());
    result["points"] = expected;
    result["points_per_second"] = expected / result.at("wall_seconds").get<double>();
    return result;
}
} // namespace

int main(int argc, char** argv)
{
    try
    {
        if (argc == 2 && std::string(argv[1]) == "--describe")
        {
            std::cout << json({{"entwine_version", ENTWINE_VERSION_STRING},
                {"pdal_version", pdal::Config::versionString()},
                {"build_type", ENTWINE_BENCHMARK_BUILD_TYPE},
                {"cxx_flags", ENTWINE_BENCHMARK_FLAGS},
                {"library_path", ENTWINE_BENCHMARK_LIBRARY},
                {"compiler", ENTWINE_BENCHMARK_COMPILER}}).dump() << std::endl;
            return 0;
        }
        check(argc == 2, "Usage: entwine-benchmark REQUEST.json | --describe");
        const json request = readJson(argv[1]);
        const std::string operation = request.at("operation");
        json result;
        if (operation == "generate") result = generate(request);
        else if (operation == "inspect")
            result = {{"points", checkSources(analyze(
                request.at("input").get<StringList>(),
                json::array({json::object()}), false, request.at("tmp"), {}, 1, false))}};
        else
        {
            check(operation == "sample", "Unknown operation");
            result = sample(request);
        }
        std::ofstream stream(request.at("result").get<std::string>());
        stream << result.dump(2) << std::endl;
        check(stream.good(), "Cannot write result");
        return 0;
    }
    catch (const std::exception& e)
    {
        std::cerr << "Benchmark failed: " << e.what() << std::endl;
        return 1;
    }
}
